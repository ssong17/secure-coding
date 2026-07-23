import os
import sqlite3
import uuid
import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, g
from flask_socketio import SocketIO, send, join_room, emit
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret!'
DATABASE = 'market.db'
socketio = SocketIO(app)

# isoformat 시각 문자열(예: 2026-07-23T06:04:01.180464)을 "2026-07-23 06:04:01" 형태로 표시
@app.template_filter('fmt_datetime')
def fmt_datetime(value):
    if not value:
        return '-'
    return value.replace('T', ' ')[:19]

# 상품 이미지 업로드 설정
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
ALLOWED_IMAGE_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 업로드 용량 5MB 제한
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_image(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

# 업로드된 이미지를 검증 후 uuid 파일명으로 저장, 저장된 파일명을 반환 (파일이 없으면 None)
def save_product_image(image_file):
    if not image_file or not image_file.filename:
        return None
    if not allowed_image(image_file.filename):
        raise ValueError('png, jpg, jpeg, gif, webp 형식의 이미지만 업로드할 수 있습니다.')
    ext = secure_filename(image_file.filename).rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4()}.{ext}"
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    image_file.save(os.path.join(UPLOAD_FOLDER, filename))
    return filename

# 상품 이미지 파일 삭제 (존재할 때만)
def delete_product_image(filename):
    if not filename:
        return
    path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(path):
        os.remove(path)

# 데이터베이스 연결 관리: 요청마다 연결 생성 후 사용, 종료 시 close
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row  # 결과를 dict처럼 사용하기 위함
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

# 현재 로그인한 사용자가 관리자인지 DB 기준으로 확인 (일반 라우트에서 관리자 여부에 따라 동작을 분기할 때 사용)
def current_user_is_admin():
    if 'user_id' not in session:
        return False
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT is_admin FROM user WHERE id = ?", (session['user_id'],))
    row = cursor.fetchone()
    return bool(row and row['is_admin'])

# 관리자 전용 라우트 보호 데코레이터 (세션이 아닌 DB의 is_admin을 기준으로 매번 검증)
def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT is_admin FROM user WHERE id = ?", (session['user_id'],))
        row = cursor.fetchone()
        if not row or not row['is_admin']:
            flash('관리자만 접근할 수 있습니다.')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated

# 신고 누적에 따른 자동 조치 기준
AUTO_SUSPEND_REPORT_THRESHOLD = 3       # 수락된 신고가 이 횟수 이상이면 대상 회원 자동 휴면
AUTO_SUSPEND_FALSE_REPORT_THRESHOLD = 3  # 거절된(허위) 신고가 이 횟수 이상이면 신고자 자동 휴면

# 회원 본인에 대한 신고 + 본인이 등록한 상품에 대한 신고를 합산해 누적 수락 신고 수를 계산
def count_accepted_reports_against_user(cursor, user_id):
    cursor.execute("""
        SELECT COUNT(*) AS c FROM report
        WHERE status = 'accepted' AND (
            (target_type = 'user' AND target_id = ?)
            OR (target_type = 'product' AND target_id IN (SELECT id FROM product WHERE seller_id = ?))
        )
    """, (user_id, user_id))
    return cursor.fetchone()['c']

# 신고 대상 회원의 누적 수락 신고 수(본인 + 본인 상품)를 확인해 기준치 이상이면 자동 휴면 처리 (관리자 계정 제외)
def maybe_auto_suspend_reported_user(cursor, target_id):
    if count_accepted_reports_against_user(cursor, target_id) < AUTO_SUSPEND_REPORT_THRESHOLD:
        return
    cursor.execute("SELECT is_admin, is_suspended FROM user WHERE id = ?", (target_id,))
    target = cursor.fetchone()
    if target and not target['is_admin'] and not target['is_suspended']:
        cursor.execute("UPDATE user SET is_suspended = 1 WHERE id = ?", (target_id,))

# 신고자의 누적 거절(허위) 신고 수를 확인해 기준치 이상이면 자동 휴면 처리 (관리자 계정 제외)
def maybe_auto_suspend_false_reporter(cursor, reporter_id):
    cursor.execute(
        "SELECT COUNT(*) AS c FROM report WHERE reporter_id = ? AND status = 'rejected'",
        (reporter_id,)
    )
    if cursor.fetchone()['c'] < AUTO_SUSPEND_FALSE_REPORT_THRESHOLD:
        return
    cursor.execute("SELECT is_admin, is_suspended FROM user WHERE id = ?", (reporter_id,))
    reporter = cursor.fetchone()
    if reporter and not reporter['is_admin'] and not reporter['is_suspended']:
        cursor.execute("UPDATE user SET is_suspended = 1 WHERE id = ?", (reporter_id,))

# 테이블 생성 (최초 실행 시에만)
def init_db():
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        # 사용자 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                bio TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0,
                is_suspended INTEGER NOT NULL DEFAULT 0
            )
        """)
        # 기존 DB에 is_admin/is_suspended 컬럼이 없으면 추가 (마이그레이션)
        cursor.execute("PRAGMA table_info(user)")
        user_columns = [row[1] for row in cursor.fetchall()]
        if 'is_admin' not in user_columns:
            cursor.execute("ALTER TABLE user ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
        if 'is_suspended' not in user_columns:
            cursor.execute("ALTER TABLE user ADD COLUMN is_suspended INTEGER NOT NULL DEFAULT 0")
        # 관리자 계정이 하나도 없으면 기본 관리자 계정 시드 생성
        cursor.execute("SELECT 1 FROM user WHERE is_admin = 1")
        if cursor.fetchone() is None:
            cursor.execute(
                "INSERT OR IGNORE INTO user (id, username, password, is_admin) VALUES (?, ?, ?, 1)",
                ('admin', '관리자', 'admin1234')
            )
        # 상품 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS product (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price TEXT NOT NULL,
                seller_id TEXT NOT NULL
            )
        """)
        # 기존 DB에 image/is_hidden 컬럼이 없으면 추가 (마이그레이션)
        cursor.execute("PRAGMA table_info(product)")
        product_columns = [row[1] for row in cursor.fetchall()]
        if 'image' not in product_columns:
            cursor.execute("ALTER TABLE product ADD COLUMN image TEXT")
        if 'is_hidden' not in product_columns:
            cursor.execute("ALTER TABLE product ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0")
        # 신고 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id TEXT PRIMARY KEY,
                reporter_id TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT 'user',
                target_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL DEFAULT ''
            )
        """)
        # 기존 DB에 target_type/status/created_at 컬럼이 없으면 추가 (마이그레이션)
        cursor.execute("PRAGMA table_info(report)")
        report_columns = [row[1] for row in cursor.fetchall()]
        if 'target_type' not in report_columns:
            cursor.execute("ALTER TABLE report ADD COLUMN target_type TEXT NOT NULL DEFAULT 'user'")
        if 'status' not in report_columns:
            cursor.execute("ALTER TABLE report ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
        if 'created_at' not in report_columns:
            cursor.execute("ALTER TABLE report ADD COLUMN created_at TEXT NOT NULL DEFAULT ''")
        # 1:1 대화방 테이블 생성 (참여자 두 명을 정렬된 순서로 저장해 중복 생성 방지)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation (
                id TEXT PRIMARY KEY,
                user_a_id TEXT NOT NULL,
                user_b_id TEXT NOT NULL,
                UNIQUE(user_a_id, user_b_id)
            )
        """)
        # 1:1 채팅 메시지 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS message (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                sender_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # 전체 채팅(그룹 채팅) 메시지 테이블 생성 (관리자 모니터링용으로 서버에 보존)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_message (
                id TEXT PRIMARY KEY,
                sender_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.commit()

# 두 사용자 간 대화방을 조회하고 없으면 새로 생성
def get_or_create_conversation(db, user_a_id, user_b_id):
    cursor = db.cursor()
    p1, p2 = sorted([user_a_id, user_b_id])
    cursor.execute(
        "SELECT id FROM conversation WHERE user_a_id = ? AND user_b_id = ?", (p1, p2)
    )
    conv = cursor.fetchone()
    if conv:
        return conv['id']
    conversation_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO conversation (id, user_a_id, user_b_id) VALUES (?, ?, ?)",
        (conversation_id, p1, p2)
    )
    db.commit()
    return conversation_id

# 기본 라우트
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')

# 회원가입
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        user_id = request.form['id'].strip()
        username = request.form['username'].strip()
        password = request.form['password']
        db = get_db()
        cursor = db.cursor()
        # 아이디 중복 체크
        cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 아이디입니다.')
            return redirect(url_for('register'))
        # 사용자이름(닉네임) 중복 체크
        cursor.execute("SELECT * FROM user WHERE username = ?", (username,))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 사용자이름입니다.')
            return redirect(url_for('register'))
        cursor.execute("INSERT INTO user (id, username, password) VALUES (?, ?, ?)",
                       (user_id, username, password))
        db.commit()
        flash('회원가입이 완료되었습니다. 로그인 해주세요.')
        return redirect(url_for('login'))
    return render_template('register.html')

# 로그인
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        user_id = request.form['id']
        password = request.form['password']
        db = get_db()
        cursor = db.cursor()
        cursor.execute("SELECT * FROM user WHERE id = ? AND password = ?", (user_id, password))
        user = cursor.fetchone()
        if user:
            if user['is_suspended']:
                flash('휴면 처리된 계정입니다. 관리자에게 문의해주세요.')
                return redirect(url_for('login'))
            session['user_id'] = user['id']
            session['is_admin'] = bool(user['is_admin'])
            flash('로그인 성공!')
            return redirect(url_for('dashboard'))
        else:
            flash('아이디 또는 비밀번호가 올바르지 않습니다.')
            return redirect(url_for('login'))
    return render_template('login.html')

# 로그아웃
@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('is_admin', None)
    flash('로그아웃되었습니다.')
    return redirect(url_for('index'))

PRODUCTS_PER_PAGE = 10

# 대시보드: 사용자 정보와 상품 리스트를 페이지당 10개씩 표시
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    # 현재 사용자 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()

    page = request.args.get('page', 1, type=int)
    if not page or page < 1:
        page = 1
    offset = (page - 1) * PRODUCTS_PER_PAGE

    # 상품 조회: 관리자는 숨김 상품까지 모두, 일반 사용자는 숨겨지지 않은 상품만
    if current_user['is_admin']:
        cursor.execute("SELECT COUNT(*) AS c FROM product")
        total_count = cursor.fetchone()['c']
        cursor.execute("SELECT * FROM product ORDER BY rowid DESC LIMIT ? OFFSET ?", (PRODUCTS_PER_PAGE, offset))
    else:
        cursor.execute("SELECT COUNT(*) AS c FROM product WHERE is_hidden = 0")
        total_count = cursor.fetchone()['c']
        cursor.execute(
            "SELECT * FROM product WHERE is_hidden = 0 ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (PRODUCTS_PER_PAGE, offset)
        )
    page_products = cursor.fetchall()
    total_pages = max(1, (total_count + PRODUCTS_PER_PAGE - 1) // PRODUCTS_PER_PAGE)

    return render_template(
        'dashboard.html', products=page_products, user=current_user,
        page=page, total_pages=total_pages
    )

# 프로필 페이지: bio 업데이트 가능
@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        bio = request.form.get('bio', '')
        if not username:
            flash('사용자이름을 입력해주세요.')
            return redirect(url_for('profile'))
        # 사용자이름(닉네임) 중복 체크 (본인 제외)
        cursor.execute("SELECT * FROM user WHERE username = ? AND id != ?", (username, session['user_id']))
        if cursor.fetchone() is not None:
            flash('이미 존재하는 사용자이름입니다.')
            return redirect(url_for('profile'))
        cursor.execute("UPDATE user SET username = ?, bio = ? WHERE id = ?", (username, bio, session['user_id']))
        db.commit()
        flash('프로필이 업데이트되었습니다.')
        return redirect(url_for('profile'))
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    return render_template('profile.html', user=current_user)

# 마이페이지: 비밀번호 변경
@app.route('/profile/password', methods=['POST'])
def update_password():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_password = request.form.get('current_password', '')
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    if current_user['password'] != current_password:
        flash('현재 비밀번호가 일치하지 않습니다.')
        return redirect(url_for('profile'))
    if not new_password:
        flash('새 비밀번호를 입력해주세요.')
        return redirect(url_for('profile'))
    if new_password != confirm_password:
        flash('새 비밀번호가 일치하지 않습니다.')
        return redirect(url_for('profile'))
    cursor.execute("UPDATE user SET password = ? WHERE id = ?", (new_password, session['user_id']))
    db.commit()
    flash('비밀번호가 변경되었습니다.')
    return redirect(url_for('profile'))

# 사용자 조회: 아이디로 검색
@app.route('/users')
def users():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    query = request.args.get('q', '').strip()
    db = get_db()
    cursor = db.cursor()
    # 관리자 계정은 아이디 유출 방지를 위해 조회 대상에서 제외
    if query:
        cursor.execute(
            "SELECT id, username, bio, is_suspended FROM user "
            "WHERE (username LIKE ? OR id LIKE ?) AND is_admin = 0 ORDER BY username",
            ('%' + query + '%', '%' + query + '%')
        )
    else:
        cursor.execute("SELECT id, username, bio, is_suspended FROM user WHERE is_admin = 0 ORDER BY username")
    results = cursor.fetchall()
    return render_template('users.html', users=results, query=query)

# 사용자 상세 정보 조회 (관리자 계정은 아이디 유출 방지를 위해 조회 불가)
@app.route('/user/<user_id>')
def user_detail(user_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username, bio, is_admin, is_suspended FROM user WHERE id = ?", (user_id,))
    found_user = cursor.fetchone()
    if not found_user or found_user['is_admin']:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    report_count = None
    if current_user_is_admin():
        report_count = count_accepted_reports_against_user(cursor, user_id)
    return render_template(
        'user_detail.html', user=found_user,
        report_count=report_count, auto_suspend_threshold=AUTO_SUSPEND_REPORT_THRESHOLD
    )

# 관리자: 회원 휴면 해제 (휴면 전환은 신고 누적에 따라 자동으로만 이뤄지고, 해제는 관리자가 수동으로 처리)
@app.route('/admin/users/<user_id>/unsuspend', methods=['POST'])
@admin_required
def admin_unsuspend_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    target = cursor.fetchone()
    if not target:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    if target['is_admin'] or target['id'] == session['user_id']:
        flash('관리자 계정은 대상이 될 수 없습니다.')
        return redirect(url_for('user_detail', user_id=user_id))
    cursor.execute("UPDATE user SET is_suspended = 0 WHERE id = ?", (user_id,))
    db.commit()
    flash('휴면이 해제되었습니다.')
    return redirect(url_for('user_detail', user_id=user_id))

# 관리자: 회원 휴면 재전환 (해제 후에도 누적 수락 신고가 기준치 이상으로 남아있으면 다시 휴면 처리 가능)
@app.route('/admin/users/<user_id>/suspend', methods=['POST'])
@admin_required
def admin_suspend_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    target = cursor.fetchone()
    if not target:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    if target['is_admin'] or target['id'] == session['user_id']:
        flash('관리자 계정은 대상이 될 수 없습니다.')
        return redirect(url_for('user_detail', user_id=user_id))
    if count_accepted_reports_against_user(cursor, user_id) < AUTO_SUSPEND_REPORT_THRESHOLD:
        flash('누적 수락 신고가 기준치 미만이라 휴면 전환할 수 없습니다.')
        return redirect(url_for('user_detail', user_id=user_id))
    cursor.execute("UPDATE user SET is_suspended = 1 WHERE id = ?", (user_id,))
    db.commit()
    flash('휴면 계정으로 전환되었습니다.')
    return redirect(url_for('user_detail', user_id=user_id))

# 관리자: 회원 강제 탈퇴 (등록된 상품/이미지도 함께 삭제)
@app.route('/admin/users/<user_id>/delete', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    target = cursor.fetchone()
    if not target:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    if target['is_admin'] or target['id'] == session['user_id']:
        flash('관리자 계정은 강제 탈퇴시킬 수 없습니다.')
        return redirect(url_for('user_detail', user_id=user_id))
    cursor.execute("SELECT id, image FROM product WHERE seller_id = ?", (user_id,))
    for p in cursor.fetchall():
        delete_product_image(p['image'])
        cursor.execute("DELETE FROM product WHERE id = ?", (p['id'],))
    cursor.execute("DELETE FROM user WHERE id = ?", (user_id,))
    db.commit()
    flash('사용자가 강제 탈퇴 처리되었습니다.')
    return redirect(url_for('users'))

# 1:1 채팅방 입장 (없으면 생성 후 대화 내역과 함께 렌더링, ?product_id= 로 들어오면 해당 상품을 대화방에 연결)
@app.route('/chat/<peer_id>')
def chat(peer_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if peer_id == session['user_id']:
        flash('자기 자신과는 채팅할 수 없습니다.')
        return redirect(url_for('users'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username FROM user WHERE id = ?", (peer_id,))
    peer = cursor.fetchone()
    if not peer:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    conversation_id = get_or_create_conversation(db, session['user_id'], peer_id)

    # 상품 상세의 "문의하기"로 들어온 경우, 해당 상품(판매자가 peer일 때만)을 대화방에 연결
    product_id_param = request.args.get('product_id')
    if product_id_param:
        cursor.execute("SELECT id FROM product WHERE id = ? AND seller_id = ?", (product_id_param, peer_id))
        if cursor.fetchone():
            cursor.execute("UPDATE conversation SET product_id = ? WHERE id = ?", (product_id_param, conversation_id))
            db.commit()

    cursor.execute("SELECT product_id FROM conversation WHERE id = ?", (conversation_id,))
    linked_product_id = cursor.fetchone()['product_id']
    linked_product = None
    if linked_product_id:
        cursor.execute("SELECT * FROM product WHERE id = ?", (linked_product_id,))
        linked_product = cursor.fetchone()

    cursor.execute("""
        SELECT m.content, m.created_at, m.sender_id, u.username AS sender_name
        FROM message m JOIN user u ON m.sender_id = u.id
        WHERE m.conversation_id = ?
        ORDER BY m.created_at
    """, (conversation_id,))
    history = cursor.fetchall()
    return render_template(
        'chat.html', peer=peer, conversation_id=conversation_id,
        history=history, my_id=session['user_id'], linked_product=linked_product
    )

# 상품 구매 (= 상품 가격만큼 판매자에게 송금). 잔액/판매 여부를 조건부 UPDATE로 원자적으로 검증해
# 동시 요청에도 잔액이 음수가 되거나 같은 상품이 중복 판매되지 않도록 함
@app.route('/product/<product_id>/buy', methods=['POST'])
def buy_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    if product['seller_id'] == session['user_id']:
        flash('본인이 등록한 상품은 구매할 수 없습니다.')
        return redirect(url_for('view_product', product_id=product_id))
    if product['is_sold']:
        flash('이미 판매 완료된 상품입니다.')
        return redirect(url_for('view_product', product_id=product_id))
    if product['is_hidden']:
        flash('구매할 수 없는 상품입니다.')
        return redirect(url_for('view_product', product_id=product_id))
    if not product['price'].isdigit() or int(product['price']) <= 0:
        flash('상품 가격 정보가 올바르지 않아 구매할 수 없습니다.')
        return redirect(url_for('view_product', product_id=product_id))
    amount = int(product['price'])

    # 잔액 확인과 차감을 하나의 조건부 UPDATE로 원자적으로 처리 (TOCTOU 경쟁 상태 방지)
    cursor.execute(
        "UPDATE user SET balance = balance - ? WHERE id = ? AND balance >= ?",
        (amount, session['user_id'], amount)
    )
    if cursor.rowcount == 0:
        db.rollback()
        flash('잔액이 부족합니다.')
        return redirect(url_for('view_product', product_id=product_id))

    # 판매완료 처리도 조건부 UPDATE로 원자 처리 (동시에 여러 명이 구매를 시도해도 한 명만 성공)
    cursor.execute("UPDATE product SET is_sold = 1 WHERE id = ? AND is_sold = 0", (product_id,))
    if cursor.rowcount == 0:
        db.rollback()
        flash('이미 판매 완료된 상품입니다.')
        return redirect(url_for('view_product', product_id=product_id))

    cursor.execute("UPDATE user SET balance = balance + ? WHERE id = ?", (amount, product['seller_id']))
    transfer_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO transfer (id, sender_id, receiver_id, amount, product_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (transfer_id, session['user_id'], product['seller_id'], amount, product_id, datetime.datetime.utcnow().isoformat())
    )
    db.commit()
    flash(f'{product["title"]}을(를) {amount:,}원에 구매했습니다.')
    return redirect(url_for('view_product', product_id=product_id))

# 상품 등록
@app.route('/product/new', methods=['GET', 'POST'])
def new_product():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        price = request.form['price']

        try:
            image_filename = save_product_image(request.files.get('image'))
        except ValueError as e:
            flash(str(e))
            return redirect(url_for('new_product'))

        db = get_db()
        cursor = db.cursor()
        product_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO product (id, title, description, price, seller_id, image) VALUES (?, ?, ?, ?, ?, ?)",
            (product_id, title, description, price, session['user_id'], image_filename)
        )
        db.commit()
        flash('상품이 등록되었습니다.')
        return redirect(url_for('dashboard'))
    return render_template('new_product.html')

# 상품 수정 (판매자 본인만 가능, 새 사진을 올리면 기존 사진을 교체)
@app.route('/product/<product_id>/edit', methods=['GET', 'POST'])
def edit_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    if product['seller_id'] != session['user_id']:
        flash('본인이 등록한 상품만 수정할 수 있습니다.')
        return redirect(url_for('view_product', product_id=product_id))

    if request.method == 'POST':
        title = request.form['title']
        description = request.form['description']
        price = request.form['price']

        try:
            new_filename = save_product_image(request.files.get('image'))
        except ValueError as e:
            flash(str(e))
            return redirect(url_for('edit_product', product_id=product_id))

        if new_filename:
            delete_product_image(product['image'])
            image_filename = new_filename
        else:
            image_filename = product['image']

        cursor.execute(
            "UPDATE product SET title = ?, description = ?, price = ?, image = ? WHERE id = ?",
            (title, description, price, image_filename, product_id)
        )
        db.commit()
        flash('상품이 수정되었습니다.')
        return redirect(url_for('view_product', product_id=product_id))

    return render_template('edit_product.html', product=product)

# 상품 삭제 (판매자 본인 또는 관리자만 가능)
@app.route('/product/<product_id>/delete', methods=['POST'])
def delete_product(product_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    if product['seller_id'] != session['user_id'] and not current_user_is_admin():
        flash('본인이 등록한 상품만 삭제할 수 있습니다.')
        return redirect(url_for('view_product', product_id=product_id))
    delete_product_image(product['image'])
    cursor.execute("DELETE FROM product WHERE id = ?", (product_id,))
    db.commit()
    flash('상품이 삭제되었습니다.')
    return redirect(url_for('dashboard'))

# 관리자: 상품 숨기기/숨김 해제 (신고된 상품 차단 용도로도 사용)
@app.route('/admin/products/<product_id>/toggle-hide', methods=['POST'])
@admin_required
def admin_toggle_hide_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    new_status = 0 if product['is_hidden'] else 1
    cursor.execute("UPDATE product SET is_hidden = ? WHERE id = ?", (new_status, product_id))
    db.commit()
    flash('상품이 숨김 처리되었습니다.' if new_status else '숨김이 해제되었습니다.')
    return redirect(url_for('view_product', product_id=product_id))

# 상품 상세보기 (숨김 처리된 상품은 판매자 본인과 관리자만 볼 수 있음)
@app.route('/product/<product_id>')
def view_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    is_owner = 'user_id' in session and product['seller_id'] == session['user_id']
    is_admin = current_user_is_admin()
    if product['is_hidden'] and not is_owner and not is_admin:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    # 판매자 정보 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
    seller = cursor.fetchone()
    return render_template('view_product.html', product=product, seller=seller, is_admin=is_admin)

# 신고하기 (사용자/상품 상세 페이지의 "신고하기" 링크로 대상 유형·id가 미리 채워져서 들어올 수 있음)
@app.route('/report', methods=['GET', 'POST'])
def report():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    if request.method == 'POST':
        target_type = request.form.get('target_type')
        target_id = request.form.get('target_id', '').strip()
        reason = request.form.get('reason', '').strip()

        if target_type == 'user':
            cursor.execute("SELECT 1 FROM user WHERE id = ?", (target_id,))
        elif target_type == 'product':
            cursor.execute("SELECT 1 FROM product WHERE id = ?", (target_id,))
        else:
            flash('신고 대상 유형이 올바르지 않습니다.')
            return redirect(url_for('report'))

        if cursor.fetchone() is None:
            flash('신고 대상을 찾을 수 없습니다.')
            return redirect(url_for('report'))

        report_id = str(uuid.uuid4())
        created_at = datetime.datetime.utcnow().isoformat()
        cursor.execute(
            "INSERT INTO report (id, reporter_id, target_type, target_id, reason, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (report_id, session['user_id'], target_type, target_id, reason, created_at)
        )
        db.commit()
        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))

    target_type = request.args.get('target_type', 'user')
    target_id = request.args.get('target_id', '')
    return render_template('report.html', target_type=target_type, target_id=target_id)

# 관리자: 신고 내역 조회 (신고자/대상/사유/처리상태, 대상·신고자의 누적 건수도 함께 표시)
@app.route('/admin/reports')
@admin_required
def admin_reports():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT r.*, u.username AS reporter_name,
               (SELECT COUNT(*) FROM report fr WHERE fr.reporter_id = r.reporter_id AND fr.status = 'rejected')
                   AS reporter_false_count,
               (SELECT COUNT(*) FROM report tr WHERE tr.status = 'accepted' AND (
                   (tr.target_type = 'user' AND tr.target_id = r.target_id)
                   OR (tr.target_type = 'product' AND tr.target_id IN
                       (SELECT id FROM product WHERE seller_id = r.target_id))
               )) AS target_accepted_count
        FROM report r JOIN user u ON r.reporter_id = u.id
        ORDER BY r.created_at DESC
    """)
    reports = cursor.fetchall()
    return render_template(
        'admin/reports.html', reports=reports,
        auto_suspend_threshold=AUTO_SUSPEND_REPORT_THRESHOLD,
        auto_suspend_false_threshold=AUTO_SUSPEND_FALSE_REPORT_THRESHOLD
    )

# 관리자: 신고 수락 (대상 회원 또는 대상 상품의 판매자의 누적 수락 신고 수를 확인해 기준치 이상이면 자동 휴면)
@app.route('/admin/reports/<report_id>/accept', methods=['POST'])
@admin_required
def admin_accept_report(report_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM report WHERE id = ?", (report_id,))
    target_report = cursor.fetchone()
    if not target_report:
        flash('신고 내역을 찾을 수 없습니다.')
        return redirect(url_for('admin_reports'))
    cursor.execute("UPDATE report SET status = 'accepted' WHERE id = ?", (report_id,))
    if target_report['target_type'] == 'user':
        maybe_auto_suspend_reported_user(cursor, target_report['target_id'])
    elif target_report['target_type'] == 'product':
        cursor.execute("SELECT seller_id FROM product WHERE id = ?", (target_report['target_id'],))
        product = cursor.fetchone()
        if product:
            maybe_auto_suspend_reported_user(cursor, product['seller_id'])
    db.commit()
    flash('신고를 수락 처리했습니다.')
    return redirect(url_for('admin_reports'))

# 관리자: 신고 거절 (허위 신고 패널티 - 신고자의 누적 거절 수가 기준치 이상이면 자동 휴면)
@app.route('/admin/reports/<report_id>/reject', methods=['POST'])
@admin_required
def admin_reject_report(report_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM report WHERE id = ?", (report_id,))
    target_report = cursor.fetchone()
    if not target_report:
        flash('신고 내역을 찾을 수 없습니다.')
        return redirect(url_for('admin_reports'))
    cursor.execute("UPDATE report SET status = 'rejected' WHERE id = ?", (report_id,))
    maybe_auto_suspend_false_reporter(cursor, target_report['reporter_id'])
    db.commit()
    flash('신고를 거절 처리했습니다.')
    return redirect(url_for('admin_reports'))

# 관리자: 신고 내역 삭제
@app.route('/admin/reports/<report_id>/delete', methods=['POST'])
@admin_required
def admin_delete_report(report_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT 1 FROM report WHERE id = ?", (report_id,))
    if cursor.fetchone() is None:
        flash('신고 내역을 찾을 수 없습니다.')
        return redirect(url_for('admin_reports'))
    cursor.execute("DELETE FROM report WHERE id = ?", (report_id,))
    db.commit()
    flash('신고 내역이 삭제되었습니다.')
    return redirect(url_for('admin_reports'))

# 관리자 대시보드: 전체 현황 요약
@app.route('/admin')
@admin_required
def admin_dashboard():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT COUNT(*) AS c FROM user")
    user_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) AS c FROM product")
    product_count = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) AS c FROM report")
    report_count = cursor.fetchone()['c']
    return render_template(
        'admin/dashboard.html',
        user_count=user_count, product_count=product_count, report_count=report_count
    )

# 관리자: 1:1 대화방 목록 (참여자, 메시지 수, 마지막 대화 시각)
@app.route('/admin/chats')
@admin_required
def admin_chats():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT c.id, c.user_a_id, c.user_b_id,
               ua.username AS user_a_name, ub.username AS user_b_name,
               COUNT(m.id) AS message_count,
               MAX(m.created_at) AS last_message_at
        FROM conversation c
        JOIN user ua ON c.user_a_id = ua.id
        JOIN user ub ON c.user_b_id = ub.id
        LEFT JOIN message m ON m.conversation_id = c.id
        GROUP BY c.id
        ORDER BY last_message_at DESC
    """)
    conversations = cursor.fetchall()
    cursor.execute("SELECT COUNT(*) AS c FROM broadcast_message")
    broadcast_count = cursor.fetchone()['c']
    return render_template('admin/chats.html', conversations=conversations, broadcast_count=broadcast_count)

# 관리자: 특정 1:1 대화방의 전체 메시지 열람 (모니터링용, 읽기 전용)
@app.route('/admin/chats/<conversation_id>')
@admin_required
def admin_chat_detail(conversation_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT c.*, ua.username AS user_a_name, ub.username AS user_b_name
        FROM conversation c
        JOIN user ua ON c.user_a_id = ua.id
        JOIN user ub ON c.user_b_id = ub.id
        WHERE c.id = ?
    """, (conversation_id,))
    conv = cursor.fetchone()
    if not conv:
        flash('대화방을 찾을 수 없습니다.')
        return redirect(url_for('admin_chats'))
    cursor.execute("""
        SELECT m.*, u.username AS sender_name
        FROM message m JOIN user u ON m.sender_id = u.id
        WHERE m.conversation_id = ?
        ORDER BY m.created_at
    """, (conversation_id,))
    messages = cursor.fetchall()
    return render_template('admin/chat_detail.html', conv=conv, messages=messages)

# 관리자: 전체 채팅(그룹 채팅) 로그 열람 (최근 200건, 읽기 전용)
@app.route('/admin/chats/broadcast')
@admin_required
def admin_broadcast_chat():
    db = get_db()
    cursor = db.cursor()
    cursor.execute("""
        SELECT b.*, u.username AS sender_name
        FROM broadcast_message b JOIN user u ON b.sender_id = u.id
        ORDER BY b.created_at DESC
        LIMIT 200
    """)
    messages = cursor.fetchall()
    return render_template('admin/broadcast_chat.html', messages=messages)

# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트하고 관리자 모니터링을 위해 DB에 보존
# (발신자 표시는 클라이언트가 보낸 값을 쓰지 않고 세션 기준으로 서버가 직접 구성 - 사용자명 위조 방지)
@socketio.on('send_message')
def handle_send_message_event(data):
    if 'user_id' not in session:
        return
    content = (data.get('message') or '').strip()
    if not content:
        return
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT username FROM user WHERE id = ?", (session['user_id'],))
    sender = cursor.fetchone()
    if not sender:
        return
    message_id = str(uuid.uuid4())
    created_at = datetime.datetime.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO broadcast_message (id, sender_id, content, created_at) VALUES (?, ?, ?, ?)",
        (message_id, session['user_id'], content, created_at)
    )
    db.commit()
    send({
        'message_id': message_id,
        'username': f"{sender['username']}(@{session['user_id']})",
        'message': content
    }, broadcast=True)

# 대화방 참여자인지 DB 기준으로 확인 (클라이언트가 임의의 room에 join하는 것 방지)
def is_conversation_participant(conversation_id, user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute(
        "SELECT 1 FROM conversation WHERE id = ? AND (user_a_id = ? OR user_b_id = ?)",
        (conversation_id, user_id, user_id)
    )
    return cursor.fetchone() is not None

# 1:1 채팅방 입장: 본인이 참여자인 대화방에만 join 허용
@socketio.on('join_chat')
def handle_join_chat(data):
    if 'user_id' not in session:
        return
    conversation_id = data.get('conversation_id')
    if not conversation_id or not is_conversation_participant(conversation_id, session['user_id']):
        return
    join_room(conversation_id)

# 1:1 채팅 메시지 전송: DB에 저장 후 해당 대화방에만 브로드캐스트
@socketio.on('send_direct_message')
def handle_send_direct_message(data):
    if 'user_id' not in session:
        return
    conversation_id = data.get('conversation_id')
    content = (data.get('message') or '').strip()
    if not conversation_id or not content:
        return
    if not is_conversation_participant(conversation_id, session['user_id']):
        return
    db = get_db()
    cursor = db.cursor()
    message_id = str(uuid.uuid4())
    created_at = datetime.datetime.utcnow().isoformat()
    cursor.execute(
        "INSERT INTO message (id, conversation_id, sender_id, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (message_id, conversation_id, session['user_id'], content, created_at)
    )
    db.commit()
    cursor.execute("SELECT username FROM user WHERE id = ?", (session['user_id'],))
    sender = cursor.fetchone()
    emit('receive_direct_message', {
        'sender_id': session['user_id'],
        'sender_name': sender['username'],
        'message': content,
        'created_at': created_at
    }, room=conversation_id)

if __name__ == '__main__':
    init_db()  # 앱 컨텍스트 내에서 테이블 생성
    socketio.run(app, debug=True)
