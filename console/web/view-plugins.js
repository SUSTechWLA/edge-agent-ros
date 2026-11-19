// Display links only for operator-installed views that belong to this Console.
(() => {
  const slots = {
    map: document.querySelector('.map-panel .panel-header'),
    sensor: document.querySelector('.sensor-panel .panel-header'),
  };
  const links = new Map();
  const params = new URLSearchParams(location.search);

  async function read(url) {
    const result = await fetch(url, {cache:'no-store', credentials:'omit', signal:AbortSignal.timeout(2400)});
    if (!result.ok) throw new Error('view unavailable');
    return result.json();
  }

  function archive(view, eventId) {
    const query = new URLSearchParams({source:'journal:'+view.id, kind:view.id+'.started', limit:'1'});
    if (eventId) query.set('event_id', eventId);
    return read('/api/events?' + query);
  }

  async function display(view) {
    let link = links.get(view.id);
    if (!link) {
      link = document.createElement('a');
      link.id = view.id+'-link';
      link.className = 'more';
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.hidden = true;
      links.set(view.id, link);
    }
    if (link.parentElement !== slots[view.slot]) slots[view.slot].append(link);
    link.textContent = view.label + ' ↗';
    try {
      const port = Number(params.get(view.id+'Port') || view.port);
      if (!Number.isInteger(port) || port <= 0 || port > 65535) throw new Error('invalid port');
      // A local deployment without this view never probes another application's port.
      const known = await archive(view);
      if (!known.events?.length) throw new Error('no recorded source');
      const base = new URL(location.origin);
      base.port = String(port);
      const health = await read(new URL('/health', base));
      if (health.id !== view.id || !/^[a-f0-9]{32,64}$/.test(health.started_event)) throw new Error('invalid identity');
      if (known.events[0].event_id !== health.started_event) {
        const boot = await archive(view, health.started_event);
        if (boot.archive_id !== known.archive_id || !boot.events?.length) throw new Error('different source');
      }
      link.href = base.href;
      link.hidden = false;
    } catch {
      link.hidden = true;
      link.removeAttribute('href');
    }
  }

  async function refresh() {
    try {
      if (document.hidden) return;
      const definitions = await read('/view-plugins/views.json');
      if (!Array.isArray(definitions) || definitions.length > 16) throw new Error('invalid view list');
      const active = new Map();
      for (const view of definitions) {
        if (!view || typeof view.id !== 'string' || !/^[a-z][a-z0-9-]{0,47}$/.test(view.id)
            || !Object.hasOwn(slots, view.slot) || !slots[view.slot] || typeof view.label !== 'string'
            || !view.label.trim() || view.label.length > 64 || active.has(view.id)) continue;
        active.set(view.id, view);
      }
      for (const [id, link] of links) {
        if (!active.has(id)) { link.remove(); links.delete(id); }
      }
      await Promise.all(Array.from(active.values(), display));
    } catch {
      for (const link of links.values()) { link.hidden = true; link.removeAttribute('href'); }
    } finally {
      setTimeout(refresh, 8000);
    }
  }
  refresh();
})();
