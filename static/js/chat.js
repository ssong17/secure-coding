document.addEventListener('DOMContentLoaded', function () {
  var chatEl = document.getElementById('chat');
  var conversationId = chatEl.getAttribute('data-conversation-id');
  var socket = io();

  socket.on('connect', function () {
    socket.emit('join_chat', { conversation_id: conversationId });
  });

  socket.on('receive_direct_message', function (data) {
    var messages = document.getElementById('messages');
    var item = document.createElement('li');
    item.textContent = data.sender_name + '(@' + data.sender_id + '): ' + data.message;
    messages.appendChild(item);
    window.scrollTo(0, document.body.scrollHeight);
  });

  function sendMessage() {
    var input = document.getElementById('chat_input');
    var message = input.value;
    if (message) {
      socket.emit('send_direct_message', { conversation_id: conversationId, message: message });
      input.value = '';
    }
  }

  document.getElementById('chat_send_btn').addEventListener('click', sendMessage);
  document.getElementById('chat_input').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') {
      sendMessage();
    }
  });
});
