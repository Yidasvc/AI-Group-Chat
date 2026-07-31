(() => {
  "use strict";

  const state = {
    room: "codex-team",
    token: "",
    cursor: 0,
    events: new Map(),
    members: [],
    filter: "all",
    search: "",
    connected: false,
    stopped: false,
    objectUrls: new Map(),
  };

  const el = {
    app: document.querySelector("#app"),
    authGate: document.querySelector("#auth-gate"),
    authForm: document.querySelector("#auth-form"),
    authRoom: document.querySelector("#auth-room"),
    authToken: document.querySelector("#auth-token"),
    authError: document.querySelector("#auth-error"),
    roomName: document.querySelector("#room-name"),
    connectionPill: document.querySelector("#connection-pill"),
    connectionLabel: document.querySelector("#connection-label"),
    lockButton: document.querySelector("#lock-button"),
    memberList: document.querySelector("#member-list"),
    memberCount: document.querySelector("#member-count"),
    metricOnline: document.querySelector("#metric-online"),
    metricCollaborators: document.querySelector("#metric-collaborators"),
    metricMessages: document.querySelector("#metric-messages"),
    timeline: document.querySelector("#timeline"),
    timelineWrap: document.querySelector("#timeline-wrap"),
    searchInput: document.querySelector("#search-input"),
    filterButtons: [...document.querySelectorAll(".filter-button")],
    lastSync: document.querySelector("#last-sync"),
    newActivity: document.querySelector("#new-activity"),
    emptyTemplate: document.querySelector("#empty-template"),
  };

  const roomPath = (suffix) =>
    `/v1/rooms/${encodeURIComponent(state.room)}${suffix}`;

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${state.token}`,
        ...(options.headers || {}),
      },
      cache: "no-store",
    });
    if (!response.ok) {
      let message = `HTTP ${response.status}`;
      try {
        const body = await response.json();
        message = body.message || message;
      } catch (_error) {
        // Keep the status-based message.
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  function parseCredentialFragment() {
    const fragment = new URLSearchParams(location.hash.replace(/^#/, ""));
    const room = fragment.get("room");
    const token = fragment.get("token");
    if (room && token) {
      sessionStorage.setItem("cgchat.dashboard.room", room);
      sessionStorage.setItem("cgchat.dashboard.token", token);
      history.replaceState(
        null,
        "",
        `/dashboard/?room=${encodeURIComponent(room)}`,
      );
      return { room, token };
    }
    const query = new URLSearchParams(location.search);
    return {
      room:
        query.get("room") ||
        sessionStorage.getItem("cgchat.dashboard.room") ||
        "codex-team",
      token: sessionStorage.getItem("cgchat.dashboard.token") || "",
    };
  }

  function setConnection(mode, label) {
    state.connected = mode === "connected";
    el.connectionPill.dataset.state = mode;
    el.connectionLabel.textContent = label;
  }

  function showAuth(message = "") {
    state.stopped = true;
    setConnection("offline", "需要授权");
    el.authRoom.value = state.room;
    el.authToken.value = "";
    el.authError.textContent = message;
    el.authGate.hidden = false;
    el.app.setAttribute("aria-hidden", "true");
    setTimeout(() => el.authToken.focus(), 30);
  }

  function hideAuth() {
    el.authGate.hidden = true;
    el.app.removeAttribute("aria-hidden");
    el.authError.textContent = "";
  }

  function formatClock(value) {
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hour12: false,
      }).format(new Date(value));
    } catch (_error) {
      return value || "";
    }
  }

  function formatDateTime(value) {
    try {
      return new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value));
    } catch (_error) {
      return value || "";
    }
  }

  function formatBytes(size) {
    if (!Number.isFinite(Number(size))) return "未知大小";
    const units = ["B", "KiB", "MiB", "GiB"];
    let value = Number(size);
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value >= 10 || unit === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[unit]}`;
  }

  function initials(name) {
    const text = (name || "?").trim();
    return [...text].slice(-2).join("").toUpperCase();
  }

  function isOnline(member) {
    if (!member.active) return false;
    const lastSeen = new Date(member.last_seen_at).getTime();
    return Number.isFinite(lastSeen) && Date.now() - lastSeen < 95_000;
  }

  function renderMembers() {
    const ordered = [...state.members].sort((a, b) => {
      if (a.active !== b.active) return b.active - a.active;
      if (a.role !== b.role) return a.role === "admin" ? -1 : 1;
      return a.name.localeCompare(b.name, "zh-CN");
    });
    el.memberList.replaceChildren();

    ordered.forEach((member) => {
      const online = isOnline(member);
      const row = document.createElement("div");
      row.className = "member-row";

      const avatar = document.createElement("div");
      avatar.className = `member-avatar ${member.role === "admin" ? "admin" : ""} ${
        online ? "online" : ""
      }`;
      avatar.textContent = initials(member.name);

      const name = document.createElement("div");
      name.className = "member-name";
      const strong = document.createElement("strong");
      strong.textContent = member.name;
      const detail = document.createElement("span");
      detail.textContent =
        member.role === "admin"
          ? "管理员 · 全量权限"
          : member.active
            ? member.member_id
            : "已停用";
      name.append(strong, detail);

      const status = document.createElement("span");
      status.className = `member-status ${online ? "online" : ""}`;
      status.textContent = member.active ? (online ? "在线" : "空闲") : "停用";

      row.append(avatar, name, status);
      el.memberList.append(row);
    });

    const active = ordered.filter((member) => member.active);
    const online = active.filter(isOnline);
    el.memberCount.textContent = String(active.length);
    el.metricOnline.textContent = String(online.length);
    el.metricCollaborators.textContent = String(
      active.filter((member) => member.role === "collaborator").length,
    );
  }

  function visibleEvents() {
    const query = state.search.trim().toLocaleLowerCase("zh-CN");
    return [...state.events.values()]
      .sort((a, b) => a.id - b.id)
      .filter((event) => {
        if (state.filter === "broadcast" && event.recipient) return false;
        if (state.filter === "direct" && !event.recipient) return false;
        if (
          state.filter === "files" &&
          !["file", "image", "path"].includes(event.kind)
        ) {
          return false;
        }
        if (!query) return true;
        const haystack = [
          event.text,
          event.sender?.name,
          event.recipient?.name,
          event.metadata?.filename,
          event.metadata?.path,
        ]
          .filter(Boolean)
          .join(" ")
          .toLocaleLowerCase("zh-CN");
        return haystack.includes(query);
      });
  }

  function createSystemEvent(event) {
    const item = document.createElement("li");
    item.className = "system-item";
    const text = document.createElement("span");
    text.textContent = event.text || "系统事件";
    const time = document.createElement("time");
    time.dateTime = event.created_at;
    time.textContent = formatClock(event.created_at);
    item.append(text, time);
    return item;
  }

  function createAttachment(event) {
    const metadata = event.metadata || {};
    const container = document.createElement("div");
    container.className = "attachment";

    if (event.kind === "image" && metadata.file_id) {
      const image = document.createElement("img");
      image.className = "attachment-image";
      image.alt = metadata.filename || "聊天图片";
      image.loading = "lazy";
      container.append(image);
      loadImage(metadata.file_id, image);
    }

    const row = document.createElement("div");
    row.className = event.kind === "path" ? "path-row" : "attachment-row";

    if (event.kind === "path") {
      const symbol = document.createElement("span");
      symbol.className = "file-symbol";
      symbol.textContent = "PATH";
      const code = document.createElement("code");
      code.textContent = metadata.path || "路径不可用";
      code.title = metadata.path || "";
      const copy = document.createElement("button");
      copy.className = "file-action";
      copy.type = "button";
      copy.textContent = "复制路径";
      copy.addEventListener("click", async () => {
        await navigator.clipboard.writeText(metadata.path || "");
        copy.textContent = "已复制";
        setTimeout(() => {
          copy.textContent = "复制路径";
        }, 1200);
      });
      row.append(symbol, code, copy);
    } else {
      const symbol = document.createElement("span");
      symbol.className = "file-symbol";
      symbol.textContent = event.kind === "image" ? "IMG" : "FILE";
      const copy = document.createElement("div");
      copy.className = "file-copy";
      const title = document.createElement("strong");
      title.textContent = metadata.filename || "未命名文件";
      const detail = document.createElement("span");
      detail.textContent = `${formatBytes(metadata.size)} · ${
        metadata.mime_type || "application/octet-stream"
      }`;
      copy.append(title, detail);
      const download = document.createElement("button");
      download.className = "file-action";
      download.type = "button";
      download.textContent = "下载";
      download.addEventListener("click", () =>
        downloadFile(metadata.file_id, metadata.filename, download),
      );
      row.append(symbol, copy, download);
    }

    container.append(row);
    return container;
  }

  function createMessageEvent(event) {
    const item = document.createElement("li");
    const senderRole = event.sender?.role || "system";
    item.className = `message-item ${senderRole}`;
    item.dataset.eventId = String(event.id);

    const avatar = document.createElement("div");
    avatar.className = `message-avatar ${senderRole === "admin" ? "admin" : ""}`;
    avatar.textContent = initials(event.sender?.name);

    const content = document.createElement("article");
    content.className = "message-content";

    const meta = document.createElement("div");
    meta.className = "message-meta";
    const name = document.createElement("strong");
    name.textContent = event.sender?.name || "未知成员";
    const role = document.createElement("span");
    role.className = `role-label ${senderRole === "admin" ? "admin" : ""}`;
    role.textContent = senderRole === "admin" ? "ADMIN" : "COLLAB";
    const route = document.createElement("span");
    route.className = "route-label";
    route.textContent = event.recipient
      ? `定向 → ${event.recipient.name}`
      : "全员广播";
    const time = document.createElement("time");
    time.className = "message-time";
    time.dateTime = event.created_at;
    time.textContent = formatDateTime(event.created_at);
    meta.append(name, role, route, time);
    content.append(meta);

    if (event.text) {
      const bubble = document.createElement("div");
      bubble.className = "message-bubble";
      bubble.textContent = event.text;
      content.append(bubble);
    }

    if (["file", "image", "path"].includes(event.kind)) {
      content.append(createAttachment(event));
    }

    item.append(avatar, content);
    return item;
  }

  function nearBottom() {
    return (
      el.timeline.scrollHeight - el.timeline.scrollTop - el.timeline.clientHeight <
      110
    );
  }

  function scrollToLatest() {
    el.timeline.scrollTo({ top: el.timeline.scrollHeight, behavior: "smooth" });
    el.newActivity.hidden = true;
  }

  function renderTimeline({ preservePosition = false } = {}) {
    const shouldFollow = nearBottom() && !preservePosition;
    const events = visibleEvents();
    el.timeline.replaceChildren();

    if (!events.length) {
      el.timeline.append(el.emptyTemplate.content.cloneNode(true));
    } else {
      events.forEach((event) => {
        el.timeline.append(
          event.kind === "system"
            ? createSystemEvent(event)
            : createMessageEvent(event),
        );
      });
    }
    el.metricMessages.textContent = String(state.events.size);

    if (shouldFollow) {
      requestAnimationFrame(() => {
        el.timeline.scrollTop = el.timeline.scrollHeight;
      });
    }
  }

  function addEvents(events, initial = false) {
    let added = 0;
    events.forEach((event) => {
      if (!state.events.has(event.id)) {
        state.events.set(event.id, event);
        added += 1;
      }
      state.cursor = Math.max(state.cursor, event.id);
    });
    if (added) {
      const wasNearBottom = nearBottom();
      renderTimeline({ preservePosition: !initial && !wasNearBottom });
      if (!initial && !wasNearBottom) {
        el.newActivity.hidden = false;
      }
    } else if (initial) {
      renderTimeline();
    }
  }

  async function loadImage(fileId, image) {
    if (state.objectUrls.has(fileId)) {
      image.src = state.objectUrls.get(fileId);
      return;
    }
    try {
      const response = await api(roomPath(`/files/${encodeURIComponent(fileId)}`));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      state.objectUrls.set(fileId, url);
      image.src = url;
    } catch (_error) {
      image.alt = `${image.alt}（加载失败）`;
    }
  }

  async function downloadFile(fileId, filename, button) {
    if (!fileId) return;
    const oldText = button.textContent;
    button.textContent = "准备中";
    button.disabled = true;
    try {
      const response = await api(roomPath(`/files/${encodeURIComponent(fileId)}`));
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename || fileId;
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
      button.textContent = "已下载";
    } catch (_error) {
      button.textContent = "失败";
    } finally {
      button.disabled = false;
      setTimeout(() => {
        button.textContent = oldText;
      }, 1400);
    }
  }

  async function loadRoster() {
    const response = await api(roomPath("/members"));
    const body = await response.json();
    state.members = body.members || [];
    renderMembers();
  }

  async function loadHistory() {
    const response = await api(roomPath("/history?limit=500"));
    const body = await response.json();
    addEvents(body.events || [], true);
  }

  async function timelineLoop() {
    while (!state.stopped) {
      try {
        setConnection("connected", "实时连接");
        const response = await api(
          roomPath(`/timeline?after=${state.cursor}&wait=30`),
        );
        const body = await response.json();
        addEvents(body.events || []);
        state.cursor = Math.max(state.cursor, body.cursor || 0);
        el.lastSync.textContent = `最后同步 ${formatClock(new Date().toISOString())}`;
      } catch (error) {
        if (state.stopped) return;
        if (error.status === 401 || error.status === 403) {
          showAuth(error.message);
          return;
        }
        setConnection("offline", "正在重连");
        await new Promise((resolve) => setTimeout(resolve, 1800));
      }
    }
  }

  async function rosterLoop() {
    while (!state.stopped) {
      try {
        await loadRoster();
      } catch (error) {
        if (error.status === 401 || error.status === 403) return;
      }
      await new Promise((resolve) => setTimeout(resolve, 15_000));
    }
  }

  async function connect() {
    state.stopped = false;
    state.cursor = 0;
    state.events.clear();
    state.members = [];
    el.app.setAttribute("aria-busy", "true");
    el.roomName.textContent = state.room;
    setConnection("connecting", "正在连接");
    hideAuth();
    try {
      await Promise.all([loadHistory(), loadRoster()]);
      el.app.setAttribute("aria-busy", "false");
      setConnection("connected", "实时连接");
      el.lastSync.textContent = `最后同步 ${formatClock(new Date().toISOString())}`;
      timelineLoop();
      rosterLoop();
    } catch (error) {
      el.app.setAttribute("aria-busy", "false");
      showAuth(error.message || "无法连接聊天室");
    }
  }

  el.authForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const room = el.authRoom.value.trim();
    const token = el.authToken.value.trim();
    if (!room || !token) return;
    state.room = room;
    state.token = token;
    sessionStorage.setItem("cgchat.dashboard.room", room);
    sessionStorage.setItem("cgchat.dashboard.token", token);
    history.replaceState(
      null,
      "",
      `/dashboard/?room=${encodeURIComponent(room)}`,
    );
    connect();
  });

  el.lockButton.addEventListener("click", () => {
    state.stopped = true;
    state.token = "";
    sessionStorage.removeItem("cgchat.dashboard.token");
    showAuth();
  });

  el.searchInput.addEventListener("input", () => {
    state.search = el.searchInput.value;
    renderTimeline({ preservePosition: true });
  });

  el.filterButtons.forEach((button) => {
    button.addEventListener("click", () => {
      state.filter = button.dataset.filter;
      el.filterButtons.forEach((item) =>
        item.classList.toggle("active", item === button),
      );
      renderTimeline({ preservePosition: true });
    });
  });

  el.newActivity.addEventListener("click", scrollToLatest);

  el.timeline.addEventListener("scroll", () => {
    if (nearBottom()) el.newActivity.hidden = true;
  });

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      el.searchInput.focus();
    }
    if (event.key === "Escape" && document.activeElement === el.searchInput) {
      el.searchInput.value = "";
      state.search = "";
      el.searchInput.blur();
      renderTimeline({ preservePosition: true });
    }
  });

  window.addEventListener("beforeunload", () => {
    state.stopped = true;
    state.objectUrls.forEach((url) => URL.revokeObjectURL(url));
  });

  const credentials = parseCredentialFragment();
  state.room = credentials.room;
  state.token = credentials.token;
  el.authRoom.value = state.room;
  el.roomName.textContent = state.room;
  if (state.token) {
    connect();
  } else {
    showAuth();
  }
})();
