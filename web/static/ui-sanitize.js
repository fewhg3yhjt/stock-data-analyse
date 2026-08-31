/* 前端 HTML 净化（S-02：Markdown 结果与外部内容不得注入脚本）
   基于 DOMParser 的白名单过滤：只保留安全标签与安全属性，剥离脚本/事件属性/危险 URL。 */
(() => {
  const ALLOWED_TAGS = new Set([
    'p', 'br', 'hr', 'strong', 'em', 'b', 'i', 'u', 's', 'del', 'mark', 'small',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'ul', 'ol', 'li', 'dl', 'dt', 'dd', 'blockquote', 'pre', 'code',
    'a', 'img', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'caption',
    'span', 'div', 'sup', 'sub',
  ]);
  const ALLOWED_ATTRS = new Set(['href', 'title', 'alt', 'src', 'align', 'colspan', 'rowspan', 'width', 'height']);

  function sanitizeNode(node) {
    const frag = document.createDocumentFragment();
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        frag.appendChild(document.createTextNode(child.nodeValue));
        continue;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) continue;
      const tag = child.tagName.toLowerCase();
      if (!ALLOWED_TAGS.has(tag)) {
        // 剥离不允许的标签但保留其文本内容（如 script/style/iframe）
        frag.appendChild(sanitizeNode(child));
        continue;
      }
      const el = document.createElement(tag);
      for (const attr of Array.from(child.attributes)) {
        const name = attr.name.toLowerCase();
        if (!ALLOWED_ATTRS.has(name)) continue;
        const value = attr.value;
        if (name === 'href' || name === 'src') {
          const scheme = value.trim().toLowerCase();
          if (scheme.startsWith('javascript:') || scheme.startsWith('vbscript:') || scheme.startsWith('data:text/html')) continue;
          if (name === 'src' && !/^(https?:|\/|\.\.\/|data:image\/)/i.test(value)) continue;
        }
        if (name.startsWith('on')) continue;
        el.setAttribute(name, value);
      }
      el.appendChild(sanitizeNode(child));
      frag.appendChild(el);
    }
    return frag;
  }

  window.sanitizeHtml = html => {
    const doc = new DOMParser().parseFromString(String(html ?? ''), 'text/html');
    const frag = sanitizeNode(doc.body);
    const out = document.createElement('div');
    out.appendChild(frag);
    return out.innerHTML;
  };
})();