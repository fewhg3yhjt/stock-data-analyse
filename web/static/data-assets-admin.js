(() => {
  const post = (url, body = {}) => fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  }).then(response => response.json());
  const esc = window.UI_UTILS.esc;
  const showResult = data => {
    window.showUiMessage(data.status === 'success' ? '操作已提交' : (data.error || '操作失败'), data.status === 'success' ? 'success' : 'error');
    if (data.status === 'success') window.loadAssets?.();
  };
  const attach = asset => {
    const detail = document.getElementById('asset-detail');
    if (!detail) return;
    const editable = asset.kind === 'metric' && asset.editable && !asset.builtin;
    const current = (asset.versions || []).find(item => item.publish_status === 'published');
    const candidate = (asset.versions || []).find(item =>
      ['candidate', 'validated'].includes(item.publish_status) && ['PASS', 'WARNING'].includes(item.quality_status));
    const actions = [];
    if (editable) {
      actions.push(`<button class="dm-btn" data-action="edit">编辑定义</button>`);
      actions.push(`<button class="dm-btn danger" data-action="disable">停用指标</button>`);
      actions.push(`<button class="dm-btn" data-action="regenerate">重新生成</button>`);
    }
    if (candidate) actions.push(`<button class="dm-btn primary" data-action="publish">发布候选版本</button>`);
    if (current?.previous_version_id) actions.push(`<button class="dm-btn" data-action="rollback">回滚上一版本</button>`);
    if (!actions.length) return;
    detail.insertAdjacentHTML('beforeend', `<section class="dm-detail-section"><h3>管理操作</h3><div class="dm-toolbar">${actions.join('')}</div></section>`);
    detail.querySelector('[data-action="edit"]')?.addEventListener('click', async () => {
      const definition = await window.showUiInput({title: '编辑指标定义', label: '指标定义', value: asset.definition || ''});
      if (definition != null) post(`/api/data-center/assets/${encodeURIComponent(asset.asset_key)}/definition`, {
        display_name: asset.display_name, category: asset.category, definition,
        unit: asset.unit || '', producer_task: asset.producer_task,
      }).then(showResult);
    });
    detail.querySelector('[data-action="disable"]')?.addEventListener('click', async () => {
      if (await window.showUiConfirm(`停用后将不再参与后续数据生产。`, `停用 ${asset.display_name}`)) post(`/api/data-center/assets/${encodeURIComponent(asset.asset_key)}/disable`).then(showResult);
    });
    detail.querySelector('[data-action="regenerate"]')?.addEventListener('click', async () => {
      if (await window.showUiConfirm(`将重新生成该资产的数据。`, `重新生成 ${asset.display_name}`)) post(`/api/data-center/assets/${encodeURIComponent(asset.asset_key)}/regenerate`).then(showResult);
    });
    detail.querySelector('[data-action="publish"]')?.addEventListener('click', async () => {
      if (await window.showUiConfirm('发布后将成为正式版本。', '发布候选版本')) post(`/api/data-center/versions/${encodeURIComponent(candidate.version_id)}/publish`).then(showResult);
    });
    detail.querySelector('[data-action="rollback"]')?.addEventListener('click', async () => {
      if (await window.showUiConfirm('将生成新的回滚版本。', '回滚上一版本')) post(`/api/data-center/versions/${encodeURIComponent(asset.asset_key)}/${encodeURIComponent(current.partition_key)}/rollback`).then(showResult);
    });
  };
  const original = window.openAsset;
  window.openAsset = async key => {
    if (original) await original(key);
    const response = await fetch(`/api/data-center/assets/${encodeURIComponent(key)}`);
    const data = await response.json();
    if (data.status === 'success') attach(data.asset);
  };
})();
