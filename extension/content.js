/**
 * DeepSeek CLI Bridge - Content Script
 *
 * Runs inside chat.deepseek.com pages. Handles DOM operations requested
 * by the background service worker (forwarded from the Python CLI).
 *
 * DeepSeek uses a textarea with id="chat-input" for its prompt input,
 * and has a distinct DOM structure for its chat messages.
 */

(function () {
  if (window.__deepseek_bridge_injected) {
    return;
  }
  window.__deepseek_bridge_injected = true;

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    const { type } = message;

    if (type === 'ping_content') {
      sendResponse({ pong: true });
      return false;
    }

    const handlers = {
      send_prompt: () => handleSendPrompt(message.prompt),
      check_response_status: () => handleCheckResponseStatus(),
      extract_last_code_block: () => handleExtractLastCodeBlock(),
      extract_full_response: () => handleExtractFullResponse(),
    };

    const handler = handlers[type];
    if (!handler) return false;

    handler()
      .then((data) => sendResponse(data || {}))
      .catch((err) => sendResponse({ __error: true, error: err.message || String(err) }));

    return true; // Keep message channel open for async sendResponse
  });

  // ── Prompt Submission ──────────────────────────────────────────────────

  function findPromptInput() {
    // DeepSeek uses a textarea with id="chat-input"
    const selectors = [
      '#chat-input',
      'textarea#chat-input',
      'textarea[placeholder*="Send a message"]',
      'textarea[placeholder*="Message"]',
      'textarea[placeholder*="Enter"]',
      'div[contenteditable="true"]',
      'div.ProseMirror',
      '[role="textbox"]',
      'textarea',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    }
    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function findSendButton() {
    // DeepSeek uses a button with aria-label or specific classes
    const selectors = [
      'div[class*="chat-input"] button[class*="send"]',
      'button[data-testid="send-button"]',
      'button[aria-label*="Send" i]',
      'button[aria-label*="Submit" i]',
      'button[class*="send" i]',
      'form button[type="submit"]',
      'button[type="submit"]',
    ];

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return el;
    }

    // Fallback: look for SVG send icon inside buttons near the input
    const chatInputContainer = document.querySelector('#chat-input')?.closest('div[class*="chat-input"]') ||
      document.querySelector('#chat-input')?.parentElement?.parentElement;
    if (chatInputContainer) {
      const buttons = chatInputContainer.querySelectorAll('button');
      for (const btn of buttons) {
        if (isVisible(btn)) return btn;
      }
    }

    for (const sel of selectors) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  async function handleSendPrompt(prompt) {
    const inputElement = findPromptInput();
    if (!inputElement) {
      throw new Error(
        'Could not find the prompt input element. Make sure chat.deepseek.com is fully loaded and you are logged in.'
      );
    }

    const tag = inputElement.tagName.toLowerCase();
    inputElement.focus();

    if (tag === 'textarea') {
      const nativeSetter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype,
        'value'
      )?.set;
      if (nativeSetter) {
        nativeSetter.call(inputElement, prompt);
      } else {
        inputElement.value = prompt;
      }
      inputElement.dispatchEvent(new Event('input', { bubbles: true }));
      inputElement.dispatchEvent(new Event('change', { bubbles: true }));
    } else if (
      inputElement.isContentEditable ||
      inputElement.getAttribute('contenteditable') === 'true'
    ) {
      // Select all existing content
      const selection = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(inputElement);
      selection.removeAllRanges();
      selection.addRange(range);

      // Try beforeinput event
      try {
        const beforeInputEvent = new InputEvent('beforeinput', {
          bubbles: true,
          cancelable: true,
          inputType: 'insertText',
          data: prompt,
        });
        inputElement.dispatchEvent(beforeInputEvent);
      } catch (e) {
        /* ignore */
      }

      // Try execCommand
      try {
        document.execCommand('insertText', false, prompt);
      } catch (e) {
        /* ignore */
      }

      // If input is still empty or doesn't match
      if (!inputElement.textContent || inputElement.textContent.trim() !== prompt.trim()) {
        const lines = prompt.split('\n');
        inputElement.innerHTML = lines
          .map((l) => `<p>${escapeHtml(l) || '<br>'}</p>`)
          .join('');
      }

      inputElement.dispatchEvent(new Event('input', { bubbles: true }));
      inputElement.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
      inputElement.value = prompt;
      inputElement.dispatchEvent(new Event('input', { bubbles: true }));
      inputElement.dispatchEvent(new Event('change', { bubbles: true }));
    }

    await sleep(400);

    // Wait briefly for send button to become enabled
    let submitBtn = findSendButton();
    for (let i = 0; i < 15; i++) {
      if (submitBtn && !submitBtn.disabled && submitBtn.getAttribute('aria-disabled') !== 'true') {
        break;
      }
      await sleep(100);
      submitBtn = findSendButton();
    }

    // Try clicking send button
    if (submitBtn && !submitBtn.disabled && submitBtn.getAttribute('aria-disabled') !== 'true') {
      submitBtn.click();
    }

    // Also simulate Enter key
    const enterEventInit = {
      key: 'Enter',
      code: 'Enter',
      keyCode: 13,
      which: 13,
      bubbles: true,
      cancelable: true,
    };
    inputElement.dispatchEvent(new KeyboardEvent('keydown', enterEventInit));
    inputElement.dispatchEvent(new KeyboardEvent('keypress', enterEventInit));
    inputElement.dispatchEvent(new KeyboardEvent('keyup', enterEventInit));

    await sleep(300);
    submitBtn = findSendButton();
    if (submitBtn && !submitBtn.disabled && submitBtn.getAttribute('aria-disabled') !== 'true') {
      submitBtn.click();
    }

    return { submitted: true };
  }

  // ── Response Status Check ──────────────────────────────────────────────

  function isGenerating() {
    // 1. Stop/Interrupt button visible in the UI
    const stopSelectors = [
      'button[aria-label*="Stop" i]',
      'button[aria-label*="Interrupt" i]',
      'button[class*="stop" i]',
      'div[class*="stop-generating"]',
      'button[data-testid="stop-button"]',
    ];
    for (const sel of stopSelectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return true;
    }

    // 2. Streaming / loading indicators
    const streamingSelectors = [
      '.result-streaming',
      '[data-is-streaming="true"]',
      'div[class*="streaming"]',
      'div[class*="loading"]',
      'span[class*="cursor"]',
      'span.blinking-cursor',
      'div[class*="thinking"]',
    ];
    for (const sel of streamingSelectors) {
      const el = document.querySelector(sel);
      if (el && isVisible(el)) return true;
    }

    // 3. Active spinner animations
    const activeSpinners = document.querySelectorAll(
      '.animate-spin, [class*="spin"], [class*="loading-indicator"]'
    );
    for (const el of activeSpinners) {
      if (isVisible(el)) {
        // Make sure it's related to response generation, not UI chrome
        const parent = el.closest('[class*="message"], [class*="chat"], [class*="response"]');
        if (parent) return true;
      }
    }

    return false;
  }

  function getAssistantMessages() {
    // DeepSeek uses markdown-body class for assistant responses
    const selectors = [
      // DeepSeek-specific selectors
      'div[class*="markdown-body"]',
      'div[data-role="assistant"]',
      'div[class*="assistant-message"]',
      '[data-message-author-role="assistant"]',
      // Fallback generic selectors
      'div.markdown.prose',
      'div.markdown',
      'div[class*="response-content"]',
    ];

    for (const sel of selectors) {
      const els = Array.from(document.querySelectorAll(sel)).filter(isVisible);
      if (els.length > 0) return els;
    }

    // Fallback: look for message containers with specific structure
    const messagePairs = document.querySelectorAll('div[class*="message"]');
    const assistantMsgs = [];
    for (const msg of messagePairs) {
      // Skip user messages (usually contain the input or have user-specific markers)
      if (msg.querySelector('[class*="user"]') || msg.getAttribute('data-role') === 'user') {
        continue;
      }
      const content = msg.querySelector('div[class*="markdown"], div[class*="content"]');
      if (content && isVisible(content)) {
        assistantMsgs.push(content);
      }
    }
    if (assistantMsgs.length > 0) return assistantMsgs;

    return [];
  }

  async function handleCheckResponseStatus() {
    const generating = isGenerating();
    const assistantMessages = getAssistantMessages();
    const hasResponse = assistantMessages.length > 0;

    let latestResponseText = '';
    let codeBlockCount = 0;

    if (hasResponse) {
      const latestMessage = assistantMessages[assistantMessages.length - 1];
      latestResponseText = (latestMessage.innerText || latestMessage.textContent || '').trim();

      const topPres = getTopLevelCodeBlocks(latestMessage);
      codeBlockCount = topPres.length;
    } else {
      const topPres = getTopLevelCodeBlocks(document);
      codeBlockCount = topPres.length;
    }

    return {
      generating,
      hasResponse,
      responseCount: assistantMessages.length,
      latestResponseLength: latestResponseText.length,
      latestResponseSnippet: latestResponseText.slice(0, 100),
      codeBlockCount,
    };
  }

  // ── Code Block Extraction ──────────────────────────────────────────────

  function getTopLevelCodeBlocks(scope) {
    const allPres = Array.from(scope.querySelectorAll('pre')).filter(isVisible);
    // Filter out any <pre> nested inside another <pre>
    const topLevelPres = allPres.filter((pre) => {
      let parent = pre.parentElement;
      while (parent && parent !== scope && parent !== document.body) {
        if (parent.tagName && parent.tagName.toLowerCase() === 'pre') return false;
        parent = parent.parentElement;
      }
      return true;
    });

    return topLevelPres;
  }

  function findCopyButtonForPre(pre) {
    // 1. Inside the <pre> itself
    const insideBtn = pre.querySelector(
      'button[aria-label*="Copy" i], button[title*="Copy" i], button.copy-button, button[class*="copy" i]'
    );
    if (insideBtn && isVisible(insideBtn)) return insideBtn;

    // 2. In immediate parent container or header bar above <pre>
    let container = pre.parentElement;
    for (let i = 0; i < 4; i++) {
      if (!container || container === document.body) break;
      const btn = container.querySelector(
        'button[aria-label*="Copy" i], button[title*="Copy" i], button.copy-button, button[class*="copy" i]'
      );
      if (btn && isVisible(btn)) return btn;
      container = container.parentElement;
    }

    // 3. Sibling element header (e.g. previous sibling div)
    if (pre.previousElementSibling) {
      const btn = pre.previousElementSibling.querySelector(
        'button[aria-label*="Copy" i], button[title*="Copy" i], button.copy-button, button[class*="copy" i], button'
      );
      if (btn && isVisible(btn)) return btn;
    }

    // 4. Any button inside the <pre>
    const anyBtn = pre.querySelector('button');
    if (anyBtn && isVisible(anyBtn)) return anyBtn;

    return null;
  }

  async function handleExtractLastCodeBlock() {
    const assistantMessages = getAssistantMessages();
    let searchScope = document;

    if (assistantMessages.length > 0) {
      searchScope = assistantMessages[assistantMessages.length - 1];
    }

    let topPres = getTopLevelCodeBlocks(searchScope);

    // If none in the latest message, search whole document
    if (topPres.length === 0 && searchScope !== document) {
      topPres = getTopLevelCodeBlocks(document);
    }

    if (topPres.length > 0) {
      const lastPre = topPres[topPres.length - 1];

      // Strategy 1: Click the "Copy code" button on top of the code block
      const copyBtn = findCopyButtonForPre(lastPre);
      if (copyBtn) {
        try {
          copyBtn.click();
          await sleep(300);
          const copiedText = await navigator.clipboard.readText();
          if (copiedText && copiedText.trim()) {
            return { text: copiedText.trim(), method: 'copy_button' };
          }
        } catch (e) {
          console.warn('[DeepSeek CLI Bridge] Copy button / clipboard read failed:', e);
        }
      }

      // Strategy 2: Extract directly from the top-level <code> element of <pre>
      const codeEl =
        lastPre.querySelector(':scope > code') ||
        lastPre.querySelector('code') ||
        lastPre;
      let text = codeEl.innerText || codeEl.textContent || '';
      text = cleanCodeText(text);

      if (text && text.trim()) {
        return { text: text.trim(), method: 'dom_code_element' };
      }
    }

    // Fallback: If no code block found or code block was empty, use the full response text
    const fullResp = await handleExtractFullResponse();
    if (fullResp && fullResp.text) {
      return { text: fullResp.text, method: 'fallback_full_response' };
    }

    return { text: null, error: 'No code blocks or response text found' };
  }

  function cleanCodeText(text) {
    if (!text) return '';
    return text
      .replace(/^(?:[a-zA-Z0-9_#+-]+\s+)?(?:Copy|Copied|Copy code)\s*\n+/i, '')
      .trim();
  }

  // ── Full Response Extraction ───────────────────────────────────────────

  async function handleExtractFullResponse() {
    const assistantMessages = getAssistantMessages();
    if (assistantMessages.length > 0) {
      const lastEl = assistantMessages[assistantMessages.length - 1];
      const text = lastEl.innerText || lastEl.textContent;
      if (text && text.trim()) {
        return { text: text.trim() };
      }
    }

    const selectors = [
      'div[class*="markdown-body"]',
      'div[class*="message-content"]',
      'div.markdown.prose',
      'div.markdown',
      'div[class*="response"]',
    ];

    for (const sel of selectors) {
      const elements = Array.from(document.querySelectorAll(sel)).filter(isVisible);
      if (elements.length > 0) {
        const lastEl = elements[elements.length - 1];
        const text = lastEl.innerText || lastEl.textContent;
        if (text && text.trim()) {
          return { text: text.trim() };
        }
      }
    }

    return { text: null, error: 'No response containers found' };
  }

  // ── Utilities ──────────────────────────────────────────────────────────

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (
      style.display === 'none' ||
      style.visibility === 'hidden' ||
      style.opacity === '0'
    ) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0 || el.getClientRects().length > 0;
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
})();
