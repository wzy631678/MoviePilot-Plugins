import { importShared } from './__federation_fn_import.js?v=1.5.5';
// 带版本号，避免升级后浏览器仍用缓存的旧 ui.js
import { mountUI } from './ui.js?v=1.5.5';

const Vue = await importShared('vue');
const { h, ref, onMounted, onBeforeUnmount } = Vue;

// 左侧导航栏的全页入口，一个组件按 navKey 分两个视图：
//   main   → 任务墙（「定时订阅下载」）
//   search → 找资源（独立一页，不再塞在插件页的 tab 里）
// 动作（新建/编辑/立即执行/搜索下载）都复用同一份 ui.js。
const _sfc_main = {
  __name: 'PtSubscriberAppPage',
  props: {
    api: { type: Object, default: () => ({}) },
    pluginId: { type: String, default: 'PtSubscriber' },
    navKey: { type: String, default: 'main' },
  },
  emits: ['action'],
  setup(props) {
    const host = ref(null);
    let dispose = null;
    onMounted(() => {
      if (!host.value) return;
      try {
        const mode = props.navKey === 'search' ? 'search' : 'wall';
        dispose = mountUI(host.value, props.api, { mode: mode });
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
