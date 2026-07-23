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
        # 기존 DB에 image 컬럼이 없으면 추가 (마이그레이션)
        cursor.execute("PRAGMA table_info(product)")
        product_columns = [row[1] for row in cursor.fetchall()]
        if 'image' not in product_columns:
            cursor.execute("ALTER TABLE product ADD COLUMN image TEXT")
        # 신고 테이블 생성
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report (
                id TEXT PRIMARY KEY,
                reporter_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                reason TEXT NOT NULL
            )
        """)
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

# 대시보드: 사용자 정보와 전체 상품 리스트 표시
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    # 현재 사용자 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (session['user_id'],))
    current_user = cursor.fetchone()
    # 모든 상품 조회
    cursor.execute("SELECT * FROM product")
    all_products = cursor.fetchall()
    return render_template('dashboard.html', products=all_products, user=current_user)

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
    if query:
        cursor.execute(
            "SELECT id, username, bio, is_suspended FROM user WHERE username LIKE ? OR id LIKE ? ORDER BY username",
            ('%' + query + '%', '%' + query + '%')
        )
    else:
        cursor.execute("SELECT id, username, bio, is_suspended FROM user ORDER BY username")
    results = cursor.fetchall()
    return render_template('users.html', users=results, query=query)

# 사용자 상세 정보 조회
@app.route('/user/<user_id>')
def user_detail(user_id):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT id, username, bio, is_admin, is_suspended FROM user WHERE id = ?", (user_id,))
    found_user = cursor.fetchone()
    if not found_user:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    return render_template('user_detail.html', user=found_user)

# 관리자: 회원 휴면 전환/해제 (관리자 계정, 본인 제외)
@app.route('/admin/users/<user_id>/toggle-suspend', methods=['POST'])
@admin_required
def admin_toggle_suspend(user_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM user WHERE id = ?", (user_id,))
    target = cursor.fetchone()
    if not target:
        flash('사용자를 찾을 수 없습니다.')
        return redirect(url_for('users'))
    if target['is_admin'] or target['id'] == session['user_id']:
        flash('관리자 계정은 휴면 처리할 수 없습니다.')
        return redirect(url_for('user_detail', user_id=user_id))
    new_status = 0 if target['is_suspended'] else 1
    cursor.execute("UPDATE user SET is_suspended = ? WHERE id = ?", (new_status, user_id))
    db.commit()
    flash('휴면 계정으로 전환되었습니다.' if new_status else '휴면이 해제되었습니다.')
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

# 1:1 채팅방 입장 (없으면 생성 후 대화 내역과 함께 렌더링)
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
    cursor.execute("""
        SELECT m.content, m.created_at, m.sender_id, u.username AS sender_name
        FROM message m JOIN user u ON m.sender_id = u.id
        WHERE m.conversation_id = ?
        ORDER BY m.created_at
    """, (conversation_id,))
    history = cursor.fetchall()
    return render_template(
        'chat.html', peer=peer, conversation_id=conversation_id,
        history=history, my_id=session['user_id']
    )

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

# 상품 삭제 (판매자 본인만 가능)
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
    if product['seller_id'] != session['user_id']:
        flash('본인이 등록한 상품만 삭제할 수 있습니다.')
        return redirect(url_for('view_product', product_id=product_id))
    delete_product_image(product['image'])
    cursor.execute("DELETE FROM product WHERE id = ?", (product_id,))
    db.commit()
    flash('상품이 삭제되었습니다.')
    return redirect(url_for('dashboard'))

# 상품 상세보기
@app.route('/product/<product_id>')
def view_product(product_id):
    db = get_db()
    cursor = db.cursor()
    cursor.execute("SELECT * FROM product WHERE id = ?", (product_id,))
    product = cursor.fetchone()
    if not product:
        flash('상품을 찾을 수 없습니다.')
        return redirect(url_for('dashboard'))
    # 판매자 정보 조회
    cursor.execute("SELECT * FROM user WHERE id = ?", (product['seller_id'],))
    seller = cursor.fetchone()
    return render_template('view_product.html', product=product, seller=seller)

# 신고하기
@app.route('/report', methods=['GET', 'POST'])
def report():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        target_id = request.form['target_id']
        reason = request.form['reason']
        db = get_db()
        cursor = db.cursor()
        report_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO report (id, reporter_id, target_id, reason) VALUES (?, ?, ?, ?)",
            (report_id, session['user_id'], target_id, reason)
        )
        db.commit()
        flash('신고가 접수되었습니다.')
        return redirect(url_for('dashboard'))
    return render_template('report.html')

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

# 실시간 채팅: 클라이언트가 메시지를 보내면 전체 브로드캐스트
@socketio.on('send_message')
def handle_send_message_event(data):
    data['message_id'] = str(uuid.uuid4())
    send(data, broadcast=True)

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
