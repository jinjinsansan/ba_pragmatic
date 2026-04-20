const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('valhalla', {
  // Bot control
  startBot: (config) => ipcRenderer.invoke('start-bot', config),
  stopBot: () => ipcRenderer.invoke('stop-bot'),

  // Auth / billing
  authSignIn: (email, password) => ipcRenderer.invoke('auth-signin', { email, password }),
  authGetSession: () => ipcRenderer.invoke('auth-session'),
  getBillingStatus: () => ipcRenderer.invoke('billing-status'),

  // Logs / messages
  onAgentMessage: (cb) => ipcRenderer.on('agent-message', (_, msg) => cb(msg)),
  onAgentLog: (cb) => ipcRenderer.on('agent-log', (_, text) => cb(text)),

  // Window controls
  windowMinimize: () => ipcRenderer.invoke('window-minimize'),
  windowMaximize: () => ipcRenderer.invoke('window-maximize'),
  windowClose: () => ipcRenderer.invoke('window-close'),

  // Misc
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
});

// Settings モーダル (Telegram / SYSTEM タブ) 用の追加 API.
// ba の preload を参考に最小限だけ expose.
contextBridge.exposeInMainWorld('bacopy', {
  getSettings: () => ipcRenderer.invoke('get-settings'),
  saveSettings: (settings) => ipcRenderer.invoke('save-settings', settings),
  testTelegram: () => ipcRenderer.invoke('test-telegram'),
  toggleSupport: (enabled) => ipcRenderer.invoke('toggle-support', enabled),
  installDeps: () => ipcRenderer.invoke('install-deps'),
  getSupportInfo: () => ipcRenderer.invoke('get-support-info'),
  openSetupLog: () => ipcRenderer.invoke('open-setup-log'),
  onInstallDepsResult: (cb) => ipcRenderer.on('install-deps-result', (_, data) => cb(data)),
});
