/** PtSubscriber 联邦入口：Vue 模式插件页面。 */
const currentImports = {};
const exportSet = new Set(['Module', '__esModule', 'default', '_export_sfc']);
const moduleMap = {
  "./Page": () => __federation_import('./__federation_expose_Page.js?v=1.2.1').then(module =>
    Object.keys(module).every(item => exportSet.has(item)) ? () => module.default : () => module),
  "./Config": () => __federation_import('./__federation_expose_Page.js?v=1.2.1').then(module =>
    Object.keys(module).every(item => exportSet.has(item)) ? () => module.default : () => module),
  "./Dashboard": () => __federation_import('./__federation_expose_Page.js?v=1.2.1').then(module =>
    Object.keys(module).every(item => exportSet.has(item)) ? () => module.default : () => module),
  "./AppPage": () => __federation_import('./__federation_expose_Page.js?v=1.2.1').then(module =>
    Object.keys(module).every(item => exportSet.has(item)) ? () => module.default : () => module),
};

async function __federation_import(name) {
  currentImports[name] ??= import(name);
  return currentImports[name];
}

const get = (module) => {
  if (!moduleMap[module]) throw new Error('Can not find remote module ' + module);
  return moduleMap[module]();
};

const init = (shareScope) => {
  globalThis.__federation_shared__ = globalThis.__federation_shared__ || {};
  Object.entries(shareScope || {}).forEach(([key, value]) => {
    for (const [versionKey, versionValue] of Object.entries(value || {})) {
      const scope = versionValue.scope || 'default';
      globalThis.__federation_shared__[scope] = globalThis.__federation_shared__[scope] || {};
      const shared = globalThis.__federation_shared__[scope];
      (shared[key] = shared[key] || {})[versionKey] = versionValue;
    }
  });
};

export { get, init };
