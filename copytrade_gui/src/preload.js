const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('bacopy', {
  getConfig: () => ipcRenderer.invoke('get-config'),
  saveConfig: (cfg) => ipcRenderer.invoke('save-config', cfg),

  startService: (name) => ipcRenderer.invoke('start-service', name),
  stopService: (name) => ipcRenderer.invoke('stop-service', name),
  getServices: () => ipcRenderer.invoke('get-services'),

  onServiceLog: (cb) => ipcRenderer.on('service-log', (_, msg) => cb(msg)),
});
