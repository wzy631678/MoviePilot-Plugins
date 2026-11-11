import { importShared } from './__federation_fn_import.js';
// 带版本号，避免升级后浏览器仍用缓存的旧 ui.js
import { mountUI } from './ui.js?v=1.1.0';

const Vue = await importShared('vue');
const { h, ref, onMounted, onBeforeUnmount } = Vue;

const _sfc_main = {
  __name: 'PtSubscriberPage',
  props: {
    api: { type: Object, default: () => ({}) },
    pluginId: { type: String, default: 'PtSubscriber' },
  },
  emits: ['action', 'close'],
  setup(props) {
    const host = ref(null);
    let dispose = null;
    onMounted(() => {
      if (!host.value) return;
      try {
        dispose = mountUI(host.value, props.api);
      } catch (err) {
        host.value.textContent = '界面初始化失败：' + (err && err.message ? err.message : String(err));
      }
    });
    onBeforeUnmount(() => {
      if (typeof dispose === 'function') {
        try { dispose(); } catch (_) { /* noop */ }
      }
    });
    return () => h('div', { ref: host, class: 'pts-root' });
  },
};

export { _sfc_main as default };
