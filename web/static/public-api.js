(() => {
  const form = document.getElementById('api-query-form');
  if (!form) return;
  const report = document.getElementById('report');
  const fields = document.getElementById('query-fields');
  const url = document.getElementById('request-url');
  const output = document.getElementById('query-output');
  const status = document.getElementById('query-status');
  const definitions = {
    stocks: [{name:'q', label:'名称或代码关键词', placeholder:'例如：贵州茅台'}, {name:'symbol', label:'精确代码（可选）', placeholder:'例如：sh600000'}, {name:'type', label:'类型（可选）', placeholder:'stock / etf / index'}],
    sectors: [{name:'classification', label:'分类体系（可选）', placeholder:'csrc / ths_industry'}, {name:'as_of', label:'快照日期（可选）', type:'date'}],
    'stock-sectors': [{name:'symbol', label:'股票代码（可选）', placeholder:'例如：sh600000'}, {name:'sector_id', label:'板块代码（可选）', placeholder:'例如：881101'}, {name:'classification', label:'分类体系（可选）', placeholder:'csrc / ths_industry'}, {name:'as_of', label:'快照日期（可选）', type:'date'}],
    'stock-daily': [{name:'symbol', label:'股票代码（可选）', placeholder:'例如：sh600000'}, {name:'start', label:'开始日期', type:'date', required:true}, {name:'end', label:'结束日期', type:'date', required:true}],
    'industry-daily': [{name:'industry_id', label:'行业代码（可选）', placeholder:'例如：881101'}, {name:'start', label:'开始日期', type:'date', required:true}, {name:'end', label:'结束日期', type:'date', required:true}]
  };
  function escapeHtml(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function renderFields() {
    fields.innerHTML = (definitions[report.value] || []).map(item => `<div class="field"><label for="field-${item.name}">${escapeHtml(item.label)}${item.required ? ' <b>*</b>' : ''}</label><input id="field-${item.name}" name="${escapeHtml(item.name)}" type="${item.type || 'text'}" placeholder="${escapeHtml(item.placeholder || '')}" ${item.required ? 'required' : ''}></div>`).join('');
    updateUrl();
  }
  function requestUrl() {
    const params = new URLSearchParams();
    new FormData(form).forEach((value, key) => { if (key !== 'report' && value) params.set(key, value); });
    const query = params.toString();
    return `${location.origin}/api/public/${report.value}${query ? `?${query}` : ''}`;
  }
  function updateUrl() { url.textContent = requestUrl(); }
  report.addEventListener('change', renderFields);
  form.addEventListener('input', updateUrl);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const target = requestUrl();
    updateUrl(); status.className = 'query-status'; status.textContent = '正在读取已发布数据...'; output.textContent = '{\n  "status": "loading"\n}';
    try {
      const response = await fetch(target);
      const data = await response.json();
      output.textContent = JSON.stringify(data, null, 2);
      if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
      status.className = 'query-status success';
      status.textContent = `查询完成：共 ${data.total ?? data.data?.length ?? 0} 条记录。`;
    } catch (error) {
      status.className = 'query-status error'; status.textContent = `查询失败：${error.message}`;
      if (output.textContent.includes('loading')) output.textContent = JSON.stringify({status:'error', error:error.message}, null, 2);
    }
  });
  document.getElementById('copy-url').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(requestUrl()); status.className = 'query-status success'; status.textContent = '请求地址已复制。'; } catch (_) { status.className = 'query-status error'; status.textContent = '复制失败，请手动复制请求地址。'; }
  });
  document.querySelectorAll('.copy-text').forEach(button => button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = '已复制'; setTimeout(() => { button.textContent = '复制'; }, 1500); } catch (_) { button.textContent = '复制失败'; }
  }));
  renderFields();
})();
