const { app, BrowserWindow, Tray, Menu, nativeImage, screen } = require("electron");
const path = require("path");

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
  process.exit(0);
}

let win = null;
let tray = null;

function adamIcon() {
  // Simple cyan circle PNG (16x16) as tray/window icon
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAMUlEQVQ4T2NkYGD4z0ABYBzVMKoBBgYGRvL8PzIwMDAyMjL8Z2Bg+A8CowaMGjBqAABlAwQGf0mQjwAAAABJRU5ErkJggg==",
    "base64"
  );
  return nativeImage.createFromBuffer(png);
}

function placeWindow(w) {
  const { width, height } = w.getBounds();
  const display = screen.getPrimaryDisplay();
  const wa = display.workArea;
  const x = Math.max(wa.x + 20, wa.x + wa.width - width - 24);
  const y = wa.y + 24;
  w.setPosition(Math.round(x), Math.round(y));
}

function createWindow() {
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
    win.moveTop();
    return;
  }

  win = new BrowserWindow({
    width: 360,
    height: 460,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    resizable: false,
    skipTaskbar: false,
    hasShadow: true,
    show: false,
    title: "ADAM",
    icon: adamIcon(),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  win.loadFile(path.join(__dirname, "src", "index.html"));

  win.once("ready-to-show", () => {
    placeWindow(win);
    win.show();
    win.focus();
    win.moveTop();
  });

  win.on("closed", () => {
    win = null;
  });
}

function createTray() {
  if (tray) return;
  tray = new Tray(adamIcon());
  tray.setToolTip("ADAM — clic para mostrar/ocultar");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "Mostrar ADAM",
        click: () => {
          if (!win || win.isDestroyed()) createWindow();
          else {
            win.show();
            win.focus();
            win.moveTop();
          }
        },
      },
      {
        label: "Ocultar",
        click: () => win?.hide(),
      },
      { type: "separator" },
      { label: "Salir", click: () => app.quit() },
    ])
  );
  tray.on("click", () => {
    if (!win || win.isDestroyed()) createWindow();
    else if (win.isVisible()) win.hide();
    else {
      win.show();
      win.focus();
      win.moveTop();
    }
  });
}

app.on("second-instance", () => {
  if (win && !win.isDestroyed()) {
    win.show();
    win.focus();
    win.moveTop();
  } else {
    createWindow();
  }
});

app.whenReady().then(() => {
  createWindow();
  createTray();
});

app.on("window-all-closed", (e) => {
  e.preventDefault();
});
