const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('valhalla', {
  startBot: (config) => ipcRenderer.invoke('start-bot', config),
  stopBot: () => ipcRenderer.invoke('stop-bot'),
  sendCommand: (cmd) => ipcRenderer.invoke('send-command', cmd),
  getStatus: () => ipcRenderer.invoke('get-status'),

  onAgentMessage: (cb) => ipcRenderer.on('agent-message', (_, msg) => cb(msg)),
  onAgentLog: (cb) => ipcRenderer.on('agent-log', (_, text) => cb(text)),

  windowMinimize: () => ipcRenderer.invoke('window-minimize'),
  windowMaximize: () => ipcRenderer.invoke('window-maximize'),
  windowClose: () => ipcRenderer.invoke('window-close'),

  getEnv: () => ipcRenderer.invoke('get-env'),

  onUpdateStatus: (cb) => ipcRenderer.on('update-status', (_, data) => cb(data)),
  openUpdatePage: () => ipcRenderer.invoke('open-update-page'),
  checkUpdates: () => ipcRenderer.invoke('check-updates'),
  runUpdate: () => ipcRenderer.invoke('run-update'),
  runWatchdog: () => ipcRenderer.invoke('run-watchdog'),
  installDeps: () => ipcRenderer.invoke('install-deps'),
  onInstallDepsResult: (cb) => ipcRenderer.on('install-deps-result', (_, data) => cb(data)),
  openSetupLog: () => ipcRenderer.invoke('open-setup-log'),
  checkSshdInstalled: () => ipcRenderer.invoke('check-sshd-installed'),
  toggleSupport: (enabled) => ipcRenderer.invoke('toggle-support', enabled),

  checkLicense: (email) => ipcRenderer.invoke('check-license', email),
  saveCredentials: (data) => ipcRenderer.invoke('save-credentials', data),
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
});
