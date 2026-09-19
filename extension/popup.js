document.addEventListener('DOMContentLoaded', () => {
  const statusDot = document.getElementById('statusDot');
  const statusText = document.getElementById('statusText');
  const reconnectBtn = document.getElementById('reconnectBtn');

  function updateStatus(connected) {
    if (connected) {
      statusDot.classList.add('connected');
      statusText.textContent = 'Connected to CLI';
    } else {
      statusDot.classList.remove('connected');
      statusText.textContent = 'Waiting for CLI...';
    }
  }

  chrome.runtime.sendMessage({ type: 'get_status' }, (response) => {
    if (chrome.runtime.lastError) {
      console.error(chrome.runtime.lastError);
      return;
    }
    if (response) {
      updateStatus(response.connected);
    }
  });

  chrome.runtime.onMessage.addListener((message) => {
    if (message.type === 'status_update') {
      updateStatus(message.connected);
    }
  });

  reconnectBtn.addEventListener('click', () => {
    chrome.runtime.sendMessage({ type: 'reconnect' }, (response) => {
      if (chrome.runtime.lastError) {
        console.error(chrome.runtime.lastError);
      }
    });
  });
});
