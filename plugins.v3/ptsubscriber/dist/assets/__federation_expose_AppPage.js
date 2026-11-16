import { importShared } from './__federation_fn_import.js?v=1.3.6';
// 带版本号，避免升级后浏览器仍用缓存的旧 ui.js
import { mountUI } from './ui.js?v=1.3.6';

const Vue = await importShared('vue');
const { h, ref, onMounted, onBeforeUnmount } = Vue;

// 侧栏全页入口：只渲染「任务墙」视图（wall 模式），不带 tabbar / 找资源 / 运行记录。
// 点击卡片、新建/编辑、立即执行等动作仍复用同一份 ui.js，功能与配置页一致。
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
        dispose = mountUI(host.value, props.api, { mode: 'wall' });
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
