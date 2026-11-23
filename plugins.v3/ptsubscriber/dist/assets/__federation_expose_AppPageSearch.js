import { importShared } from './__federation_fn_import.js?v=1.5.4';
// 带版本号，避免升级后浏览器仍用缓存的旧 ui.js
import { mountUI } from './ui.js?v=1.5.4';

const Vue = await importShared('vue');
const { h, ref, onMounted, onBeforeUnmount } = Vue;

// 左侧导航「找资源」入口的专用组件。
// MP 加载 nav_key=search 时优先找 ./AppPageSearch；只暴露 ./AppPage 的插件在某些
// 版本上不会自动回退，结果点进去是白页（用户反馈「找资源的模板哪去了」）。
// 这里把搜索页单独暴露一份，路由命中就一定能渲染。
const _sfc_main = {
  __name: 'PtSubscriberAppPageSearch',
  props: {
    api: { type: Object, default: () => ({}) },
    pluginId: { type: String, default: 'PtSubscriber' },
    navKey: { type: String, default: 'search' },
  },
  emits: ['action'],
  setup(props) {
    const host = ref(null);
    let dispose = null;
    onMounted(() => {
      if (!host.value) return;
      try {
        dispose = mountUI(host.value, props.api, { mode: 'search' });
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
