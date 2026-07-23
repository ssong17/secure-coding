var socket = io();

socket.on('connect', function () {
  console.log('채팅 서버에 연결됨');
});

socket.on('message', function (data) {
  var messages = document.getElementById('messages');
  var item = document.createElement('li');
  item.textContent = data.username + ': ' + data.message;
  messages.appendChild(item);
  window.scrollTo(0, document.body.scrollHeight);
});

function sendMessage() {
  var input = document.getElementById('chat_input');
  var message = input.value;
  if (message) {
    socket.emit('send_message', { message: message });
    input.value = '';
  }
}

document.addEventListener('DOMContentLoaded', function () {
  document.getElementById('chat_send_btn').addEventListener('click', sendMessage);
  document.getElementById('chat_input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      sendMessage();
    }
  });
});
