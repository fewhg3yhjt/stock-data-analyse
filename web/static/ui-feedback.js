/* Shared in-page feedback and confirmation/input panels. */
(() => {
  window.showUiMessage = (message, type = '') => {
    const toast = document.getElementById('toast');
    if (!toast) return;
    toast.textContent = message || '';
    toast.dataset.type = type;
    toast.style.display = 'block';
    clearTimeout(window.__uiToastTimer);
    window.__uiToastTimer = setTimeout(() => { toast.style.display = 'none'; }, 2800);
  };
  window.alert = message => window.showUiMessage(message, 'error');

  const closePanel = (overlay, resolve, value) => {
    overlay.remove();
    resolve(value);
  };

  const createPanel = ({className, title, body, actions, input}) => new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'ui-overlay';
    overlay.style.display = 'flex';
    overlay.setAttribute('role', 'presentation');

    const panel = document.createElement('section');
    panel.className = className;
    panel.setAttribute('role', 'dialog');
    panel.setAttribute('aria-modal', 'true');
    panel.setAttribute('aria-labelledby', 'ui-panel-title');

    const header = document.createElement('div');
    header.className = `${className}-header`;
    const heading = document.createElement('h2');
    heading.id = 'ui-panel-title';
    heading.textContent = title;
    header.appendChild(heading);

    const content = document.createElement('div');
    content.className = `${className}-body`;
    if (body) {
      const text = document.createElement('p');
      text.textContent = body;
      content.appendChild(text);
    }
    if (input) {
      const field = document.createElement('div');
      field.className = 'ui-field';
      const label = document.createElement('label');
      label.textContent = input.label;
      const control = document.createElement('input');
      control.type = input.type || 'text';
      control.value = input.value || '';
      control.placeholder = input.placeholder || '';
      control.autocomplete = 'off';
      field.append(label, control);
      content.appendChild(field);
      input.control = control;
    }

    const footer = document.createElement('div');
    footer.className = `${className}-actions`;
    actions.forEach(action => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = action.primary ? 'ui-button ui-button-primary' : 'ui-button ui-button-secondary';
      button.textContent = action.label;
      button.addEventListener('click', () => closePanel(overlay, resolve, action.value));
      footer.appendChild(button);
    });

    panel.append(header, content, footer);
    overlay.appendChild(panel);
    overlay.addEventListener('click', event => {
      if (event.target === overlay) closePanel(overlay, resolve, null);
    });
    document.body.appendChild(overlay);
    const focusTarget = input ? input.control : footer.querySelector('button');
    focusTarget.focus();
    const onKeydown = event => {
      if (event.key === 'Escape') closePanel(overlay, resolve, null);
      if (event.key === 'Enter' && input && event.target === input.control) {
        closePanel(overlay, resolve, input.control.value);
      }
    };
    overlay.addEventListener('keydown', onKeydown);
  });

  window.showUiConfirm = (message, title = '请确认') => createPanel({
    className: 'ui-confirm',
    title,
    body: message,
    actions: [
      {label: '取消', value: false},
      {label: '确认', value: true, primary: true}
    ]
  });

  window.showUiInput = ({message = '', title = '请输入', label = '', value = '', type = 'text', placeholder = ''} = {}) => {
    const input = {label, value, type, placeholder};
    return createPanel({
      className: 'ui-input-panel',
      title,
      body: message,
      input,
      actions: [
        {label: '取消', value: null},
        {label: '确认', value: undefined, primary: true}
      ]
    }).then(result => result === undefined ? input.control.value : result);
  };
})();
