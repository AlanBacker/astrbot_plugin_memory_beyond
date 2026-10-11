(function () {
  "use strict";

  const bridge = window.AstrBotPluginPage || null;
  const I18N_PREFIX = "pages.memory.";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const TYPES = ["user", "feedback", "project", "reference"];
  const PARTS = ["overhead", "tools", "index", "summary", "recall", "history"];
  const FILE_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$/;
  const FRONT_RE = /^---\s*\n([\s\S]*?)\n---\s*\n?/;
  const LINK_RE = /\[\[([^\[\]\n]{1,120})\]\]/g;
  const KIND_ICON = { group: "group", private: "user", other: "chat" };

  // 简体中文文案；其他语言由 .astrbot-plugin/i18n 提供，缺失时回落到这里
  const ZH = {
    title: "记忆管理",
    subtitle: "查看与编辑机器人记住的内容",
    refresh: "刷新",
    filterPlaceholder: "搜索会话名称、ID、备注",
    filterAll: "全部",
    filterHasMemory: "有记忆",
    kindGroup: "群聊",
    kindPrivate: "私聊",
    kindOther: "其他",
    selfMemory: "机器人自身",
    globalTitle: "全局记忆",
    globalSub: "所有会话共享",
    sessionsLabel: "会话 · {n}",
    noSessions: "没有符合条件的会话",
    noBridge: "请在 AstrBot 管理面板的「插件页面」中打开此页面。",
    loadFailed: "加载失败",
    retry: "重试",
    requestFailed: "请求失败",
    welcomeTitle: "选择一个会话",
    welcomeHint: "从左侧选择会话，查看和编辑机器人在那里记住的内容。",
    statSessions: "已登记会话",
    statWithMemory: "有记忆的会话",
    statFiles: "会话记忆文件",
    statGlobal: "全局记忆",
    fileCount: "{n} 条记忆",
    enabled: "已启用",
    disabled: "未启用",
    back: "返回",
    firstSeen: "首次出现 {time}",
    lastActive: "最近活跃 {time}",
    remark: "备注",
    consolidate: "整理记忆",
    purge: "清除数据",
    memoryOffNotice: "长期记忆已在插件配置中关闭：机器人不会读写记忆，这里只是保留在磁盘上的内容。",
    globalOffNotice: "全局记忆未启用：机器人当前不会读取或写入这里的内容。如需使用，请在插件配置中开启「启用全局记忆」。",
    tabMemories: "记忆",
    tabIndex: "索引",
    tabCompression: "上下文压缩",
    tabArchives: "归档",
    searchPlaceholder: "全文搜索记忆",
    newMemory: "新建记忆",
    typeAll: "全部",
    typeOther: "未分类",
    type_user: "人物",
    type_feedback: "指导",
    type_project: "事项",
    type_reference: "资料",
    typeHint_user: "某个人是谁：身份、偏好、专长",
    typeHint_feedback: "对机器人做事方式的指导：纠正过的错误、确认过的做法",
    typeHint_project: "进行中的事、目标与约束",
    typeHint_reference: "外部资源在哪里：链接、公告、文档",
    typeMissing: "无类型",
    noDescription: "（没有描述）",
    noMemoriesTitle: "还没有记忆",
    noMemoriesHint: "机器人会在聊天中自己记下值得长期记住的事，也可以点「新建记忆」手动添加。",
    noTypeMatch: "这个分类下没有记忆",
    noSearchResults: "没有找到包含这些词的记忆",
    editorEmptyTitle: "选择一条记忆",
    editorEmptyHint: "在左侧选择一条记忆查看或编辑，或者新建一条。",
    modeForm: "表单",
    modeRaw: "原文",
    unsaved: "有未保存的修改",
    fieldFile: "文件名",
    fieldFileHint: "记人用 user-<数字ID>.md",
    fieldType: "类型",
    fieldDescription: "描述",
    fieldDescriptionHint: "一句话说明这是什么、什么时候用得上；它会成为索引里的那一行",
    fieldBody: "正文",
    fieldBodyHint: "可以用 [[文件名]] 链接到其他记忆",
    fieldRaw: "完整内容",
    fieldRawHint: "含 frontmatter 的原始文件",
    noFrontmatterNotice: "这个文件没有标准的 frontmatter（名称、描述、类型）。切换到表单编辑并保存后会自动补上。",
    linksLabel: "链接到",
    openLink: "打开这条记忆",
    linkMissing: "这条记忆不存在",
    newHint: "新记忆会在下一次对话开始或下一次压缩后进入索引",
    savedAt: "最近写入：{time}",
    delete: "删除",
    cancel: "取消",
    save: "保存",
    confirm: "确定",
    close: "关闭",
    saved: "已保存",
    deleted: "已删除",
    badFileName: "文件名只能包含字母、数字、点、下划线、连字符，并以 .md 结尾",
    needDescription: "请填写描述",
    deleteTitle: "删除记忆",
    deleteMessage: "确定删除 {file} 吗？索引里对应的行会一并移除，删除后无法恢复。",
    discardTitle: "放弃未保存的修改？",
    discardMessage: "正在编辑的内容还没有保存，离开后修改会丢失。",
    discard: "放弃修改",
    indexHealth: "索引状况",
    rebuildIndex: "按文件重建",
    indexLines: "行数",
    indexSize: "大小",
    indexTruncated: "索引超出注入上限，超出的部分不会发给模型。建议合并相关记忆、删除过时的记忆。",
    indexMissing: "{n} 个记忆文件没有出现在索引里，模型不知道它们存在：",
    indexDangling: "索引里有 {n} 行指向已不存在的文件：",
    indexHealthy: "索引与记忆文件一致。",
    indexEditorHint: "对话开始时发给模型的就是这份索引。保存记忆文件时插件会自动维护对应的行；你可以在这里调整顺序、添加分组标题。",
    watermark: "水位线",
    watermarkSub: "此前的消息已由摘要代替",
    summaryChars: "摘要长度",
    summaryCharsSub: "最近压缩：{time}",
    chars: "{n} 字",
    lastEstimate: "最近一次估算",
    ratioSub: "校准比例 {ratio}",
    lastActual: "提供商实际用量",
    lastActualSub: "最近一次 usage 上报",
    cacheHit: "提示词缓存命中",
    cacheHitSub: "最近一次请求",
    notReported: "未上报",
    chain: "摘要链路",
    chainOk: "正常",
    chainOkSub: "最近没有失败",
    chainCooling: "退避中",
    chainCoolingSub: "剩余 {left}，已连续失败 {n} 次",
    extract: "后台记忆提取",
    extractTurns: "距上次 {n} 轮",
    extractSub: "上次运行：{time}",
    lastRequest: "最近请求",
    tokens: "{n} tokens",
    partsTitle: "上下文构成（最近一次请求，估算）",
    noEstimate: "还没有请求记录",
    part_overhead: "固定开销",
    part_tools: "工具声明",
    part_index: "记忆索引",
    part_summary: "摘要",
    part_recall: "召回记忆",
    part_history: "历史原文",
    summaryTitle: "当前摘要",
    noSummary: "这个会话还没有发生过压缩。",
    resetCompression: "重置压缩状态",
    resetMessage: "将清空这个会话的摘要与水位线。已经被压缩卸下的旧对话不会回来；之后超过阈值时会基于当前历史重新摘要。",
    reset: "重置",
    liveTag: "实时",
    liveHint: "插件内存中的当前状态",
    diskTag: "磁盘记录",
    diskHint: "插件启动后这个会话还没有新消息，显示的是上次保存的状态",
    archivesTitle: "压缩归档",
    archivesHint: "每次压缩前被摘要代替的原文",
    noArchivesTitle: "还没有归档",
    noArchivesHint: "上下文压缩时，被摘要代替的原文会存在这里，方便核对摘要有没有漏掉什么。",
    archiveMeta: "{count} 条消息 · 摘要 {chars} 字",
    backToArchives: "返回归档列表",
    archiveSummary: "这次压缩生成的摘要",
    archiveMessages: "被摘要的原文（{n} 条）",
    role_user: "用户",
    role_assistant: "机器人",
    role_tool: "工具结果",
    toolCalls: "调用工具：{names}",
    emptyMessage: "（没有文本内容）",
    remarkTitle: "会话备注",
    remarkHint: "备注只在管理页显示，方便认出这个会话。留空则显示群名或昵称。",
    purgeTitle: "清除会话数据",
    purgeMessage: "「只删记忆」删除这个会话的全部记忆文件和索引；「删除全部」还会删除压缩状态、归档和会话登记。平台上的聊天记录不受影响。删除后无法恢复。",
    purgeMemories: "只删记忆",
    purgeAll: "删除全部",
    consolidateTitle: "整理记忆",
    consolidateRunning: "正在让模型复盘这里的全部记忆…",
    consolidateRunningHint: "通常需要几十秒。模型只给出修改方案，你确认后才会生效。",
    consolidateNothingTitle: "不需要整理",
    consolidateNothingHint: "模型认为这里的记忆已经很整齐了。",
    consolidateReview: "模型给出了下面的修改方案。勾选要应用的项目：写入会整文件覆盖，删除会同时移除索引行；应用时先写后删。",
    consolidateSkipped: "记忆太多，有 {n} 个文件没有参与这次整理。",
    opCreate: "新建",
    opRewrite: "改写",
    opDelete: "删除",
    previewContent: "查看新内容",
    selectedCount: "已选 {n} / {total} 项",
    applySelected: "应用所选",
    justNow: "刚刚",
    minutesAgo: "{n} 分钟前",
    hoursAgo: "{n} 小时前",
    daysAgo: "{n} 天前",
    seconds: "{n} 秒",
    minutes: "{n} 分钟",
  };

  const ICONS = {
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.9-4M4 4v4h4"/><path d="M4 13a8 8 0 0 0 14.9 4M20 20v-4h-4"/>',
    group: '<circle cx="9" cy="8" r="3.2"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.2a3 3 0 0 1 0 5.6M17.5 14a5 5 0 0 1 3 5"/>',
    user: '<circle cx="12" cy="8" r="3.6"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    chat: '<path d="M5 18l-1 3 4-1.5A8 8 0 1 0 5 18z"/>',
    bot: '<rect x="5" y="8" width="14" height="11" rx="2.5"/><path d="M12 4v4M9.5 13h.01M14.5 13h.01M9.5 16h5"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>',
    check: '<path d="M5 12.5l4.5 4.5L19 7.5"/>',
    edit: '<path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3z"/>',
    sparkles: '<path d="M11 3l1.8 4.7 4.7 1.8-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z"/><path d="M18.5 14.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
    archive: '<rect x="3" y="4" width="18" height="5" rx="1"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>',
    back: '<path d="M15 18l-6-6 6-6"/>',
    close: '<path d="M6 6l12 12M18 6L6 18"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    warn: '<path d="M12 4l9 16H3z"/><path d="M12 10v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    inbox: '<path d="M4 13l2.5-7h11l2.5 7v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/><path d="M4 13h5l1 2h4l1-2h5"/>',
    rotate: '<path d="M4 12a8 8 0 1 0 2.4-5.7L4 8.6"/><path d="M4 4v4.6h4.6"/>',
  };

  // ------------------------------------------------------------ 基础工具

  function t(key, vars) {
    const fallback = Object.prototype.hasOwnProperty.call(ZH, key) ? ZH[key] : key;
    let text = bridge ? bridge.t(I18N_PREFIX + key, fallback) : fallback;
    if (vars) {
      text = String(text).replace(/\{(\w+)\}/g, function (match, name) {
        return Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match;
      });
    }
    return text;
  }

  const PROPS = new Set(["value", "checked", "disabled", "selected"]);

  function append(el, child) {
    if (child === null || child === undefined || child === false) {
      return;
    }
    if (Array.isArray(child)) {
      child.forEach(function (item) {
        append(el, item);
      });
      return;
    }
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }

  function h(tag, attrs) {
    const el = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (name) {
        const value = attrs[name];
        if (value === null || value === undefined || value === false) {
          return;
        }
        if (name === "class") {
          el.className = value;
        } else if (name.slice(0, 2) === "on" && typeof value === "function") {
          el.addEventListener(name.slice(2).toLowerCase(), value);
        } else if (PROPS.has(name)) {
          el[name] = value;
        } else {
          el.setAttribute(name, value === true ? "" : String(value));
        }
      });
    }
    for (let i = 2; i < arguments.length; i += 1) {
      append(el, arguments[i]);
    }
    return el;
  }

  function icon(name) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = ICONS[name] || "";
    return svg;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function fmtTime(ts) {
    if (!ts) {
      return "—";
    }
    const d = new Date(ts * 1000);
    return (
      d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
    );
  }

  function relTime(ts) {
    if (!ts) {
      return "—";
    }
    const diff = Math.max(0, Date.now() / 1000 - ts);
    if (diff < 60) {
      return t("justNow");
    }
    if (diff < 3600) {
      return t("minutesAgo", { n: Math.floor(diff / 60) });
    }
    if (diff < 86400) {
      return t("hoursAgo", { n: Math.floor(diff / 3600) });
    }
    if (diff < 86400 * 30) {
      return t("daysAgo", { n: Math.floor(diff / 86400) });
    }
    return fmtTime(ts).slice(0, 10);
  }

  function fmtDuration(sec) {
    sec = Math.max(0, Math.round(sec));
    return sec < 60 ? t("seconds", { n: sec }) : t("minutes", { n: Math.ceil(sec / 60) });
  }

  function fmtNum(n) {
    return Number(n || 0).toLocaleString();
  }

  function fmtBytes(n) {
    n = Number(n || 0);
    return n < 1024 ? n + " B" : (n / 1024).toFixed(1) + " KB";
  }

  function stem(file) {
    return file.slice(-3) === ".md" ? file.slice(0, -3) : file;
  }

  function composeMemory(name, description, type, body) {
    const desc = String(description || "").replace(/\s+/g, " ").trim();
    return (
      "---\nname: " + String(name || "").trim() +
      "\ndescription: " + desc +
      "\nmetadata:\n  type: " + String(type || "").trim() +
      "\n---\n\n" + String(body || "").trim() + "\n"
    );
  }

  function parseMemory(content) {
    content = content || "";
    const match = FRONT_RE.exec(content);
    const fields = {};
    let nested = "";
    if (match) {
      let inMeta = false;
      match[1].split("\n").forEach(function (line) {
        if (!line.trim()) {
          return;
        }
        const idx = line.indexOf(":");
        if (idx < 0) {
          return;
        }
        const key = line.slice(0, idx).trim().toLowerCase();
        const value = line.slice(idx + 1).trim().replace(/^['"]+|['"]+$/g, "");
        if (/^[ \t]/.test(line)) {
          if (inMeta && key === "type") {
            nested = value;
          }
          return;
        }
        inMeta = key === "metadata" && !value;
        if ((key === "name" || key === "description" || key === "type") && !(key in fields)) {
          fields[key] = value;
        }
      });
    }
    const body = match ? content.slice(match[0].length) : content;
    return {
      description: fields.description || "",
      type: (nested || fields.type || "").toLowerCase(),
      body: body.replace(/^\n+|\n+$/g, ""),
      hasFrontmatter: Boolean(match),
    };
  }

  function extractLinks(text) {
    const found = [];
    let match;
    LINK_RE.lastIndex = 0;
    while ((match = LINK_RE.exec(text || ""))) {
      const target = match[1].trim();
      if (target && found.indexOf(target) < 0) {
        found.push(target);
      }
    }
    return found;
  }

  function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function highlight(text, query) {
    const terms = query.split(/\s+/).filter(Boolean).map(escapeRegExp);
    if (!terms.length) {
      return text;
    }
    const re = new RegExp("(" + terms.join("|") + ")", "ig");
    return String(text).split(re).map(function (part, i) {
      return i % 2 === 1 ? h("mark", null, part) : part;
    });
  }

  // ------------------------------------------------------------ 接口

  function toError(err) {
    const message = err && err.message ? err.message : String(err || "");
    return new Error(message || t("requestFailed"));
  }

  async function apiGet(endpoint, params) {
    try {
      return (await bridge.apiGet(endpoint, params || {})) || {};
    } catch (err) {
      throw toError(err);
    }
  }

  async function apiPost(endpoint, body) {
    try {
      return (await bridge.apiPost(endpoint, body || {})) || {};
    } catch (err) {
      throw toError(err);
    }
  }

  // ------------------------------------------------------------ 提示与弹窗

  function toast(message, kind) {
    const box = document.getElementById("toasts");
    const el = h(
      "div",
      { class: "toast" + (kind === "error" ? " error" : ""), role: "status" },
      icon(kind === "error" ? "warn" : "check"),
      h("div", null, message)
    );
    box.append(el);
    setTimeout(function () {
      el.remove();
    }, kind === "error" ? 6000 : 3200);
  }

  function openDialog(opts) {
    const overlay = h("div", { class: "overlay" });
    const body = h("div", { class: "dialog-body" });
    const foot = h("div", { class: "dialog-foot" });
    let resolveDone;
    const done = new Promise(function (resolve) {
      resolveDone = resolve;
    });
    const ctrl = {
      busy: false,
      done: done,
      isOpen: function () {
        return overlay.isConnected;
      },
      setBody: function (node) {
        body.replaceChildren();
        append(body, node);
      },
      setFoot: function (nodes) {
        foot.replaceChildren();
        append(foot, nodes);
      },
      close: function (value) {
        if (!overlay.isConnected) {
          return;
        }
        overlay.remove();
        document.removeEventListener("keydown", onKey);
        resolveDone(value);
      },
    };
    function onKey(event) {
      if (event.key === "Escape" && !ctrl.busy) {
        ctrl.close(null);
      }
    }
    const closeBtn = h(
      "button",
      {
        class: "btn ghost icon-only",
        title: t("close"),
        onclick: function () {
          if (!ctrl.busy) {
            ctrl.close(null);
          }
        },
      },
      icon("close")
    );
    overlay.append(
      h(
        "div",
        { class: "dialog" + (opts.wide ? " wide" : ""), role: "dialog", "aria-modal": "true" },
        h("div", { class: "dialog-head" }, h("h3", null, opts.title), closeBtn),
        body,
        foot
      )
    );
    overlay.addEventListener("mousedown", function (event) {
      if (event.target === overlay && !ctrl.busy) {
        ctrl.close(null);
      }
    });
    document.addEventListener("keydown", onKey);
    if (opts.body) {
      ctrl.setBody(opts.body);
    }
    document.body.append(overlay);
    return ctrl;
  }

  function confirmDialog(opts) {
    const ctrl = openDialog({ title: opts.title, body: h("p", null, opts.message) });
    ctrl.setFoot([
      h("button", { class: "btn", onclick: function () { ctrl.close(false); } }, t("cancel")),
      h(
        "button",
        {
          class: "btn " + (opts.danger ? "danger" : "primary"),
          onclick: function () {
            ctrl.close(true);
          },
        },
        opts.confirm || t("confirm")
      ),
    ]);
    return ctrl.done.then(function (value) {
      return value === true;
    });
  }

  function promptDialog(opts) {
    const input = h("input", {
      class: "input",
      value: opts.value || "",
      maxlength: opts.maxLength || 64,
    });
    const ctrl = openDialog({
      title: opts.title,
      body: [opts.message ? h("p", null, opts.message) : null, input],
    });
    function submit() {
      ctrl.close(input.value);
    }
    input.addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        submit();
      }
    });
    ctrl.setFoot([
      h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")),
      h("button", { class: "btn primary", onclick: submit }, t("save")),
    ]);
    setTimeout(function () {
      input.focus();
    }, 0);
    return ctrl.done.then(function (value) {
      return typeof value === "string" ? value : null;
    });
  }

  async function withBusy(button, task) {
    if (button) {
      button.disabled = true;
    }
    try {
      return await task();
    } finally {
      if (button && button.isConnected) {
        button.disabled = false;
      }
    }
  }

  // ------------------------------------------------------------ 通用块

  function emptyState(iconName, title, hint) {
    return h("div", { class: "empty" }, icon(iconName), h("strong", null, title), hint ? h("span", null, hint) : null);
  }

  function spinnerBlock() {
    return h("div", { class: "center-state" }, h("div", { class: "spinner" }));
  }

  function notice(kind, content) {
    return h(
      "div",
      { class: "notice" + (kind === "info" ? " info" : "") },
      icon(kind === "info" ? "info" : "warn"),
      h("div", null, content)
    );
  }

  function errorBlock(message, retry) {
    return h(
      "div",
      { class: "empty" },
      icon("warn"),
      h("strong", null, t("loadFailed")),
      h("span", null, message),
      retry ? h("button", { class: "btn", onclick: retry }, t("retry")) : null
    );
  }

  function typeTag(type) {
    if (TYPES.indexOf(type) >= 0) {
      return h("span", { class: "tag " + type, title: t("typeHint_" + type) }, t("type_" + type));
    }
    return type ? h("span", { class: "tag" }, type) : h("span", { class: "tag warn" }, t("typeMissing"));
  }

  function stat(label, value, sub, extra) {
    return h(
      "div",
      { class: "stat" },
      h("div", { class: "stat-label" }, label),
      h("div", { class: "stat-value" }, value),
      sub ? h("div", { class: "stat-sub" }, sub) : null,
      extra || null
    );
  }

  // ------------------------------------------------------------ 状态

  const state = {
    overview: null,
    overviewError: "",
    filterText: "",
    filterKind: "all",
    selected: null,
    detail: null,
    detailError: "",
    tab: "memories",
    typeFilter: "all",
    fileQuery: "",
    searchResults: null,
    editor: null,
    indexDraft: null,
    compression: null,
    compressionLoading: false,
    archives: null,
    archivesLoading: false,
    archiveView: null,
  };
  const els = {};
  let searchTimer = 0;
  let detailToken = 0;

  function scopeParams() {
    return { scope: state.selected.scope, key: state.selected.key || "" };
  }

  function isSelected(sel) {
    return Boolean(
      state.selected && sel &&
      state.selected.scope === sel.scope &&
      (state.selected.key || "") === (sel.key || "")
    );
  }

  function resetDetailState() {
    state.detail = null;
    state.detailError = "";
    state.tab = "memories";
    state.typeFilter = "all";
    state.fileQuery = "";
    state.searchResults = null;
    state.editor = null;
    state.indexDraft = null;
    state.compression = null;
    state.compressionLoading = false;
    state.archives = null;
    state.archivesLoading = false;
    state.archiveView = null;
  }

  function hasUnsaved() {
    const indexDirty = Boolean(
      state.detail && state.indexDraft !== null && state.indexDraft !== state.detail.index.content
    );
    return Boolean((state.editor && state.editor.dirty) || indexDirty);
  }

  async function confirmDiscard() {
    if (!hasUnsaved()) {
      return true;
    }
    const ok = await confirmDialog({
      title: t("discardTitle"),
      message: t("discardMessage"),
      confirm: t("discard"),
      danger: true,
    });
    if (ok) {
      if (state.editor) {
        state.editor.dirty = false;
      }
      state.indexDraft = null;
    }
    return ok;
  }

  function kindLabel(kind) {
    return t(kind === "group" ? "kindGroup" : kind === "private" ? "kindPrivate" : "kindOther");
  }

  function sessionTitle(s) {
    return s.remark || s.name || (s.target ? kindLabel(s.kind) + " " + s.target : s.key);
  }

  function sessionSub(s) {
    const bits = [];
    if (s.remark || s.name) {
      bits.push(s.target);
    }
    bits.push(s.platform);
    const when = relTime(s.lastActive || s.updatedAt);
    if (when !== "—") {
      bits.push(when);
    }
    return bits.filter(Boolean).join(" · ");
  }

  // ------------------------------------------------------------ 加载

  async function loadOverview(quiet) {
    try {
      state.overview = await apiGet("overview");
      state.overviewError = "";
    } catch (err) {
      if (quiet && state.overview) {
        toast(err.message, "error");
        return;
      }
      state.overviewError = err.message;
    }
    renderSessionList();
    if (!state.selected) {
      renderMain();
    }
  }

  async function loadDetail() {
    const sel = state.selected;
    if (!sel) {
      return;
    }
    detailToken += 1;
    const token = detailToken;
    try {
      const data = await apiGet("scope", scopeParams());
      if (token !== detailToken) {
        return;
      }
      state.detail = data;
      state.detailError = "";
    } catch (err) {
      if (token !== detailToken) {
        return;
      }
      state.detailError = err.message;
    }
    renderMain();
  }

  async function refreshAll() {
    if (!(await confirmDiscard())) {
      return;
    }
    state.compression = null;
    state.archives = null;
    state.archiveView = null;
    state.searchResults = null;
    state.fileQuery = "";
    if (state.editor && !state.editor.isNew) {
      const file = state.editor.oldFile;
      state.editor = null;
      await Promise.all([loadOverview(true), loadDetail()]);
      if (state.detail && state.detail.files.some(function (f) { return f.file === file; })) {
        await openFile(file);
      }
      return;
    }
    state.editor = null;
    await Promise.all([loadOverview(true), loadDetail()]);
  }

  async function selectScope(sel) {
    if (isSelected(sel) && state.detail) {
      els.app.classList.add("show-detail");
      return;
    }
    if (!(await confirmDiscard())) {
      return;
    }
    state.selected = sel;
    resetDetailState();
    els.app.classList.add("show-detail");
    renderSessionList();
    renderMain();
    await loadDetail();
  }

  function backToList() {
    els.app.classList.remove("show-detail");
  }

  // ------------------------------------------------------------ 外壳与会话列表

  function buildShell() {
    const app = document.getElementById("app");
    app.replaceChildren();
    els.app = app;
    document.title = t("title");
    els.filterInput = h("input", {
      class: "input",
      type: "search",
      placeholder: t("filterPlaceholder"),
      value: state.filterText,
      oninput: function (event) {
        state.filterText = event.target.value;
        renderSessionList();
      },
    });
    els.filterChips = h("div", { class: "chips" });
    els.sessionList = h("div", { class: "session-list" });
    els.main = h("main", { class: "main" });
    els.sidebar = h(
      "aside",
      { class: "sidebar" },
      h(
        "div",
        { class: "sidebar-head" },
        h(
          "div",
          { class: "brand" },
          h("div", null, h("h1", null, t("title")), h("p", null, t("subtitle"))),
          h("button", { class: "btn ghost icon-only", title: t("refresh"), onclick: refreshAll }, icon("refresh"))
        ),
        h("div", { class: "search-box" }, icon("search"), els.filterInput),
        els.filterChips
      ),
      els.sessionList
    );
    app.append(els.sidebar, els.main);
    if (state.selected) {
      app.classList.add("show-detail");
    }
    renderFilterChips();
    renderSessionList();
    renderMain();
  }

  function renderFilterChips() {
    const items = [
      ["all", "filterAll"],
      ["group", "kindGroup"],
      ["private", "kindPrivate"],
      ["memory", "filterHasMemory"],
    ];
    els.filterChips.replaceChildren();
    items.forEach(function (item) {
      els.filterChips.append(
        h(
          "button",
          {
            class: "chip" + (state.filterKind === item[0] ? " active" : ""),
            onclick: function () {
              state.filterKind = item[0];
              renderFilterChips();
              renderSessionList();
            },
          },
          t(item[1])
        )
      );
    });
  }

  function filteredSessions() {
    if (!state.overview) {
      return [];
    }
    const query = state.filterText.trim().toLowerCase();
    return state.overview.sessions
      .filter(function (s) {
        if (state.filterKind === "group" && s.kind !== "group") {
          return false;
        }
        if (state.filterKind === "private" && s.kind !== "private") {
          return false;
        }
        if (state.filterKind === "memory" && !s.fileCount) {
          return false;
        }
        if (!query) {
          return true;
        }
        return [s.remark, s.name, s.target, s.platform, s.umo, s.key].some(function (v) {
          return String(v || "").toLowerCase().indexOf(query) >= 0;
        });
      })
      .sort(function (a, b) {
        return Math.max(b.lastActive, b.updatedAt) - Math.max(a.lastActive, a.updatedAt);
      });
  }

  function renderSessionList() {
    const list = els.sessionList;
    if (!list) {
      return;
    }
    list.replaceChildren();
    if (!state.overview) {
      list.append(
        state.overviewError
          ? errorBlock(state.overviewError, function () { loadOverview(false); })
          : spinnerBlock()
      );
      return;
    }
    const g = state.overview.global;
    const globalActive = isSelected({ scope: "global", key: "" });
    list.append(h("div", { class: "list-label" }, t("selfMemory")));
    list.append(
      h(
        "button",
        {
          class: "session-item" + (globalActive ? " active" : ""),
          onclick: function () {
            selectScope({ scope: "global", key: "" });
          },
        },
        h("div", { class: "avatar global" }, icon("bot")),
        h(
          "div",
          { class: "session-text" },
          h("div", { class: "session-name" }, t("globalTitle")),
          h("div", { class: "session-sub" }, g.enabled ? t("globalSub") : t("disabled"))
        ),
        h("span", { class: "count" }, g.fileCount)
      )
    );
    const sessions = filteredSessions();
    list.append(h("div", { class: "list-label" }, t("sessionsLabel", { n: sessions.length })));
    if (!sessions.length) {
      list.append(h("div", { class: "empty", style: "padding:24px 12px" }, h("span", null, t("noSessions"))));
    }
    sessions.forEach(function (s) {
      const active = isSelected({ scope: "session", key: s.key });
      list.append(
        h(
          "button",
          {
            class: "session-item" + (active ? " active" : ""),
            title: s.umo || s.key,
            onclick: function () {
              selectScope({ scope: "session", key: s.key });
            },
          },
          h("div", { class: "avatar " + s.kind }, icon(KIND_ICON[s.kind] || "chat")),
          h(
            "div",
            { class: "session-text" },
            h("div", { class: "session-name" }, sessionTitle(s)),
            h("div", { class: "session-sub" }, sessionSub(s))
          ),
          h("span", { class: "count" }, s.fileCount)
        )
      );
    });
  }

  // ------------------------------------------------------------ 主区域

  function renderMain() {
    const main = els.main;
    if (!main) {
      return;
    }
    main.replaceChildren();
    els.tabBody = null;
    els.fileList = null;
    els.editor = null;
    if (!state.selected) {
      main.append(renderWelcome());
      return;
    }
    if (state.detailError) {
      main.append(
        h(
          "div",
          { class: "detail" },
          h("button", { class: "btn ghost mobile-only", onclick: backToList }, icon("back"), t("back")),
          errorBlock(state.detailError, loadDetail)
        )
      );
      return;
    }
    if (!state.detail) {
      main.append(spinnerBlock());
      return;
    }
    main.append(renderDetail());
  }

  function renderWelcome() {
    const ov = state.overview;
    const box = h("div", { class: "detail" });
    if (ov) {
      const withMemory = ov.sessions.filter(function (s) { return s.fileCount > 0; });
      const files = ov.sessions.reduce(function (sum, s) { return sum + (s.fileCount || 0); }, 0);
      box.append(
        h(
          "div",
          { class: "stats", style: "margin-bottom:24px" },
          stat(t("statSessions"), fmtNum(ov.sessions.length)),
          stat(t("statWithMemory"), fmtNum(withMemory.length)),
          stat(t("statFiles"), fmtNum(files)),
          stat(t("statGlobal"), ov.global.enabled ? t("enabled") : t("disabled"), t("fileCount", { n: fmtNum(ov.global.fileCount) }))
        )
      );
      if (!ov.memoryEnabled) {
        box.append(notice("warn", t("memoryOffNotice")));
      }
    }
    box.append(h("div", { class: "panel", style: "margin-top:16px" }, emptyState("inbox", t("welcomeTitle"), t("welcomeHint"))));
    return box;
  }

  function renderDetail() {
    const d = state.detail;
    const isGlobal = d.scope === "global";
    const s = d.session || {};
    const meta = [];
    if (isGlobal) {
      meta.push(h("span", null, t("globalSub")));
      meta.push(h("span", { class: "tag " + (d.enabled ? "ok" : "warn") }, d.enabled ? t("enabled") : t("disabled")));
    } else {
      meta.push(h("span", null, kindLabel(s.kind)));
      if (s.platform) {
        meta.push(h("span", null, s.platform));
      }
      if (s.target) {
        meta.push(h("span", null, "ID ", h("code", null, s.target)));
      }
      if (s.lastActive) {
        meta.push(h("span", { title: fmtTime(s.lastActive) }, t("lastActive", { time: relTime(s.lastActive) })));
      }
      if (s.firstSeen) {
        meta.push(h("span", { title: fmtTime(s.firstSeen) }, t("firstSeen", { time: fmtTime(s.firstSeen).slice(0, 10) })));
      }
    }

    const actions = [];
    if (!isGlobal) {
      actions.push(h("button", { class: "btn", onclick: editRemark }, icon("edit"), t("remark")));
    }
    actions.push(
      h(
        "button",
        { class: "btn", onclick: consolidateScope, disabled: !d.files.length },
        icon("sparkles"),
        t("consolidate")
      )
    );
    if (!isGlobal) {
      actions.push(h("button", { class: "btn danger", onclick: purgeSession }, icon("trash"), t("purge")));
    }

    const title = isGlobal ? t("globalTitle") : sessionTitle(s);
    const head = h(
      "div",
      { class: "detail-head" },
      h(
        "div",
        { class: "detail-title" },
        h("button", { class: "btn ghost icon-only mobile-only", title: t("back"), onclick: backToList }, icon("back")),
        h("div", { class: "avatar " + (isGlobal ? "global" : s.kind) }, icon(isGlobal ? "bot" : KIND_ICON[s.kind] || "chat")),
        h(
          "div",
          { style: "min-width:0" },
          h("h2", { title: isGlobal ? "" : s.umo || s.key }, title),
          h("div", { class: "meta-row" }, meta)
        )
      ),
      h("div", { class: "actions" }, actions)
    );

    const notices = [];
    if (state.overview && !state.overview.memoryEnabled) {
      notices.push(notice("warn", t("memoryOffNotice")));
    }
    if (isGlobal && !d.enabled) {
      notices.push(notice("warn", t("globalOffNotice")));
    }

    const tabs = [["memories", "tabMemories", d.files.length], ["index", "tabIndex", null]];
    if (!isGlobal) {
      tabs.push(["compression", "tabCompression", null]);
      tabs.push(["archives", "tabArchives", d.archiveCount || 0]);
    }
    const tabBar = h("div", { class: "tabs", role: "tablist" });
    tabs.forEach(function (tab) {
      tabBar.append(
        h(
          "button",
          {
            class: "tab" + (state.tab === tab[0] ? " active" : ""),
            role: "tab",
            onclick: function () {
              if (state.tab === tab[0]) {
                return;
              }
              state.tab = tab[0];
              tabBar.querySelectorAll(".tab").forEach(function (el, i) {
                el.classList.toggle("active", tabs[i][0] === state.tab);
              });
              renderTab();
            },
          },
          t(tab[1]),
          tab[2] === null ? null : h("span", { class: "count" }, tab[2])
        )
      );
    });

    els.tabBody = h("div");
    renderTab();
    return h("div", { class: "detail" }, head, notices, tabBar, els.tabBody);
  }

  function renderTab() {
    const body = els.tabBody;
    if (!body) {
      return;
    }
    body.replaceChildren();
    els.fileList = null;
    els.editor = null;
    if (state.tab === "memories") {
      body.append(renderMemoryTab());
    } else if (state.tab === "index") {
      body.append(renderIndexTab());
    } else if (state.tab === "compression") {
      body.append(renderCompressionTab());
    } else if (state.tab === "archives") {
      body.append(renderArchiveTab());
    }
  }

  // ------------------------------------------------------------ 记忆标签页

  function renderMemoryTab() {
    els.fileList = h("div", { class: "file-list" });
    els.typeChips = h("div", { class: "chips" });
    const search = h("input", {
      class: "input",
      type: "search",
      placeholder: t("searchPlaceholder"),
      value: state.fileQuery,
      oninput: onFileQuery,
    });
    const pane = h(
      "section",
      { class: "panel file-pane" },
      h(
        "div",
        { class: "file-tools" },
        h(
          "div",
          { class: "row" },
          h("div", { class: "search-box" }, icon("search"), search),
          h("button", { class: "btn primary", onclick: newMemory }, icon("plus"), t("newMemory"))
        ),
        els.typeChips
      ),
      els.fileList
    );
    els.editor = h("section", { class: "panel editor" });
    renderTypeChips();
    renderFileList();
    renderEditor();
    return h("div", { class: "mem-layout" }, pane, els.editor);
  }

  function renderTypeChips() {
    const files = state.detail.files;
    const counts = { all: files.length, other: 0 };
    TYPES.forEach(function (type) {
      counts[type] = 0;
    });
    files.forEach(function (f) {
      if (TYPES.indexOf(f.type) >= 0) {
        counts[f.type] += 1;
      } else {
        counts.other += 1;
      }
    });
    const items = [["all", t("typeAll")]].concat(
      TYPES.map(function (type) { return [type, t("type_" + type)]; })
    );
    if (counts.other) {
      items.push(["other", t("typeOther")]);
    }
    els.typeChips.replaceChildren();
    items.forEach(function (item) {
      els.typeChips.append(
        h(
          "button",
          {
            class: "chip" + (state.typeFilter === item[0] ? " active" : ""),
            title: TYPES.indexOf(item[0]) >= 0 ? t("typeHint_" + item[0]) : null,
            onclick: function () {
              state.typeFilter = item[0];
              renderTypeChips();
              renderFileList();
            },
          },
          item[1] + " " + counts[item[0]]
        )
      );
    });
  }

  function fileItem(f, excerpt) {
    const ed = state.editor;
    const active = ed && !ed.isNew && ed.oldFile === f.file;
    return h(
      "button",
      {
        class: "file-item" + (active ? " active" : ""),
        onclick: function () {
          openFile(f.file);
        },
      },
      h(
        "div",
        { class: "file-item-top" },
        h("span", { class: "file-name" }, f.file),
        typeTag(f.type),
        h("span", { class: "file-age", title: fmtTime(f.mtime) }, relTime(f.mtime))
      ),
      h(
        "div",
        { class: "file-desc" },
        excerpt ? highlight(excerpt, state.fileQuery) : f.description || t("noDescription")
      )
    );
  }

  function renderFileList() {
    const list = els.fileList;
    if (!list) {
      return;
    }
    list.replaceChildren();
    const files = state.detail.files;
    if (state.fileQuery.trim()) {
      if (!state.searchResults) {
        list.append(h("div", { class: "empty" }, h("div", { class: "spinner" })));
        return;
      }
      if (!state.searchResults.length) {
        list.append(h("div", { class: "empty" }, h("span", null, t("noSearchResults"))));
      }
      state.searchResults.forEach(function (r) {
        const header = files.find(function (f) { return f.file === r.file; }) || { file: r.file };
        list.append(fileItem(header, r.excerpt));
      });
      return;
    }
    if (!files.length) {
      list.append(emptyState("inbox", t("noMemoriesTitle"), t("noMemoriesHint")));
      return;
    }
    const shown = files
      .filter(function (f) {
        if (state.typeFilter === "all") {
          return true;
        }
        if (state.typeFilter === "other") {
          return TYPES.indexOf(f.type) < 0;
        }
        return f.type === state.typeFilter;
      })
      .sort(function (a, b) {
        return b.mtime - a.mtime;
      });
    if (!shown.length) {
      list.append(h("div", { class: "empty" }, h("span", null, t("noTypeMatch"))));
    }
    shown.forEach(function (f) {
      list.append(fileItem(f));
    });
  }

  function onFileQuery(event) {
    state.fileQuery = event.target.value;
    clearTimeout(searchTimer);
    state.searchResults = null;
    if (!state.fileQuery.trim()) {
      renderFileList();
      return;
    }
    renderFileList();
    searchTimer = setTimeout(runSearch, 300);
  }

  async function runSearch() {
    const query = state.fileQuery.trim();
    const sel = state.selected;
    if (!query) {
      return;
    }
    let results = [];
    try {
      const res = await apiGet("search", Object.assign(scopeParams(), { q: query }));
      results = res.results || [];
    } catch (err) {
      toast(err.message, "error");
      return;
    }
    if (!isSelected(sel) || query !== state.fileQuery.trim()) {
      return;
    }
    state.searchResults = results;
    renderFileList();
  }

  function editorTitle() {
    const ed = state.editor;
    return [
      ed.dirty ? h("span", { class: "dirty-dot", title: t("unsaved") }) : null,
      ed.isNew ? t("newMemory") : ed.oldFile,
    ];
  }

  function markDirty() {
    const ed = state.editor;
    if (ed && !ed.dirty) {
      ed.dirty = true;
      if (els.editorTitle) {
        els.editorTitle.replaceChildren();
        append(els.editorTitle, editorTitle());
      }
    }
  }

  function field(label, hint, control) {
    return h("div", { class: "field" }, h("label", null, h("span", null, label), hint ? h("small", null, hint) : null), control);
  }

  function typeSegment() {
    const seg = h("div", { class: "segmented" });
    function draw() {
      seg.replaceChildren();
      TYPES.forEach(function (type) {
        seg.append(
          h(
            "button",
            {
              type: "button",
              class: state.editor.type === type ? "active" : "",
              title: t("typeHint_" + type),
              onclick: function () {
                if (state.editor.type !== type) {
                  state.editor.type = type;
                  markDirty();
                  draw();
                }
              },
            },
            t("type_" + type)
          )
        );
      });
    }
    draw();
    return seg;
  }

  function switchMode(mode) {
    const ed = state.editor;
    if (ed.mode === mode) {
      return;
    }
    if (mode === "raw") {
      ed.content = composeMemory(stem(ed.file.trim()), ed.description, ed.type, ed.body);
    } else {
      const doc = parseMemory(ed.content);
      ed.description = doc.description;
      ed.type = TYPES.indexOf(doc.type) >= 0 ? doc.type : ed.type || "user";
      ed.body = doc.hasFrontmatter ? doc.body : ed.content.trim();
    }
    ed.mode = mode;
    renderEditor();
  }

  function linkChip(name) {
    const file = name.slice(-3) === ".md" ? name : name + ".md";
    const exists = state.detail.files.some(function (f) { return f.file === file; });
    return h(
      "button",
      {
        class: "link-chip" + (exists ? "" : " missing"),
        title: exists ? t("openLink") : t("linkMissing"),
        disabled: !exists,
        onclick: function () {
          openFile(file);
        },
      },
      name
    );
  }

  function renderEditor() {
    const box = els.editor;
    if (!box) {
      return;
    }
    box.replaceChildren();
    els.editorTitle = null;
    const ed = state.editor;
    if (!ed) {
      box.append(emptyState("file", t("editorEmptyTitle"), t("editorEmptyHint")));
      return;
    }
    if (ed.loading) {
      box.append(spinnerBlock());
      return;
    }
    els.editorTitle = h("h3", { class: "file-name", style: "flex:1" }, editorTitle());
    const modes = h("div", { class: "segmented" });
    [["fields", t("modeForm")], ["raw", t("modeRaw")]].forEach(function (item) {
      modes.append(
        h(
          "button",
          {
            type: "button",
            class: ed.mode === item[0] ? "active" : "",
            onclick: function () {
              switchMode(item[0]);
            },
          },
          item[1]
        )
      );
    });
    const fileInput = h("input", {
      class: "input",
      value: ed.file,
      placeholder: "user-123456789.md",
      spellcheck: "false",
      autocomplete: "off",
      oninput: function (event) {
        ed.file = event.target.value;
        markDirty();
      },
    });
    const body = h("div", { class: "panel-body" });
    if (ed.mode === "fields") {
      if (!ed.isNew && !ed.hasFrontmatter) {
        body.append(notice("info", t("noFrontmatterNotice")));
      }
      body.append(
        h(
          "div",
          { class: "field-row" },
          field(t("fieldFile"), t("fieldFileHint"), fileInput),
          field(t("fieldType"), null, typeSegment())
        ),
        field(
          t("fieldDescription"),
          t("fieldDescriptionHint"),
          h("input", {
            class: "input",
            value: ed.description,
            maxlength: 300,
            oninput: function (event) {
              ed.description = event.target.value;
              markDirty();
            },
          })
        ),
        field(
          t("fieldBody"),
          t("fieldBodyHint"),
          h("textarea", {
            class: "textarea",
            value: ed.body,
            spellcheck: "false",
            oninput: function (event) {
              ed.body = event.target.value;
              markDirty();
            },
          })
        )
      );
    } else {
      body.append(
        field(t("fieldFile"), t("fieldFileHint"), fileInput),
        field(
          t("fieldRaw"),
          t("fieldRawHint"),
          h("textarea", {
            class: "textarea",
            style: "min-height:380px",
            value: ed.content,
            spellcheck: "false",
            oninput: function (event) {
              ed.content = event.target.value;
              markDirty();
            },
          })
        )
      );
    }
    const links = extractLinks(ed.mode === "fields" ? ed.body : ed.content);
    if (links.length) {
      body.append(h("div", { class: "links" }, icon("link"), t("linksLabel"), links.map(linkChip)));
    }
    const saveBtn = h("button", { class: "btn primary", onclick: function () { saveFile(saveBtn); } }, icon("check"), t("save"));
    const foot = h(
      "div",
      { class: "editor-foot" },
      h("span", { class: "hint" }, ed.isNew ? t("newHint") : t("savedAt", { time: fmtTime(ed.baseMtime) })),
      h(
        "div",
        { class: "actions" },
        ed.isNew ? null : h("button", { class: "btn danger", onclick: deleteFile }, icon("trash"), t("delete")),
        h("button", { class: "btn", onclick: cancelEdit }, t("cancel")),
        saveBtn
      )
    );
    box.append(h("div", { class: "panel-head" }, els.editorTitle, modes), body, foot);
  }

  function editorFromFile(data) {
    return {
      isNew: false,
      oldFile: data.file,
      file: data.file,
      mode: data.hasFrontmatter ? "fields" : "raw",
      type: TYPES.indexOf(data.type) >= 0 ? data.type : "user",
      description: data.description || "",
      body: data.hasFrontmatter ? data.body || "" : String(data.content || "").trim(),
      content: data.content || "",
      baseMtime: data.mtime || 0,
      hasFrontmatter: Boolean(data.hasFrontmatter),
      dirty: false,
    };
  }

  function revealEditor() {
    if (els.editor && window.innerWidth <= 1100) {
      els.editor.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }

  async function openFile(file) {
    if (!(await confirmDiscard())) {
      return;
    }
    const sel = state.selected;
    state.editor = { loading: true, isNew: false, oldFile: file };
    renderFileList();
    renderEditor();
    revealEditor();
    try {
      const data = await apiGet("file", Object.assign(scopeParams(), { file: file }));
      if (!isSelected(sel) || !state.editor || state.editor.oldFile !== file) {
        return;
      }
      state.editor = editorFromFile(data);
    } catch (err) {
      // 期间已切会话、或编辑器已换成别的文件 / 新建草稿：不能把它清掉
      if (!isSelected(sel) || !state.editor || !state.editor.loading || state.editor.oldFile !== file) {
        return;
      }
      state.editor = null;
      toast(err.message, "error");
    }
    renderFileList();
    renderEditor();
  }

  async function newMemory() {
    if (!(await confirmDiscard())) {
      return;
    }
    state.editor = {
      isNew: true,
      oldFile: "",
      file: "",
      mode: "fields",
      type: "user",
      description: "",
      body: "",
      content: "",
      baseMtime: 0,
      hasFrontmatter: true,
      dirty: false,
    };
    renderFileList();
    renderEditor();
    revealEditor();
  }

  async function cancelEdit() {
    if (!(await confirmDiscard())) {
      return;
    }
    state.editor = null;
    renderFileList();
    renderEditor();
  }

  async function saveFile(button) {
    const ed = state.editor;
    if (!ed || ed.loading) {
      return;
    }
    let file = ed.file.trim();
    if (file && file.slice(-3) !== ".md") {
      file += ".md";
    }
    if (!FILE_RE.test(file) || file === "MEMORY.md") {
      toast(t("badFileName"), "error");
      return;
    }
    if (ed.mode === "fields" && !ed.description.trim()) {
      toast(t("needDescription"), "error");
      return;
    }
    const payload = Object.assign(scopeParams(), {
      file: file,
      oldFile: ed.isNew ? "" : ed.oldFile,
      mode: ed.mode,
      type: ed.type,
      description: ed.description,
      body: ed.body,
      content: ed.content,
      baseMtime: ed.isNew ? null : ed.baseMtime,
    });
    await withBusy(button, async function () {
      try {
        const res = await apiPost("file/save", payload);
        ed.dirty = false;
        toast(res.message || t("saved"));
        await Promise.all([loadDetail(), loadOverview(true)]);
        await openFile(res.file || file);
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function deleteFile() {
    const ed = state.editor;
    const ok = await confirmDialog({
      title: t("deleteTitle"),
      message: t("deleteMessage", { file: ed.oldFile }),
      confirm: t("delete"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    try {
      const res = await apiPost("file/delete", Object.assign(scopeParams(), { file: ed.oldFile }));
      state.editor = null;
      toast(res.message || t("deleted"));
      await Promise.all([loadDetail(), loadOverview(true)]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  // ------------------------------------------------------------ 索引标签页

  function meterStat(label, value, ratio) {
    const level = ratio > 1 ? " danger" : ratio > 0.8 ? " warn" : "";
    return stat(
      label,
      value,
      null,
      h("div", { class: "meter" + level }, h("span", { style: "width:" + Math.min(100, ratio * 100).toFixed(1) + "%" }))
    );
  }

  function renderIndexTab() {
    const idx = state.detail.index;
    const draft = state.indexDraft !== null ? state.indexDraft : idx.content;
    const rebuildBtn = h(
      "button",
      { class: "btn small", onclick: function () { rebuildIndex(rebuildBtn); } },
      icon("refresh"),
      t("rebuildIndex")
    );
    const issues = [];
    if (idx.truncated) {
      issues.push(notice("warn", t("indexTruncated")));
    }
    if (idx.missing.length) {
      issues.push(
        notice("warn", [
          t("indexMissing", { n: idx.missing.length }),
          h("ul", { class: "kv-list" }, idx.missing.map(function (f) { return h("li", null, f); })),
        ])
      );
    }
    if (idx.dangling.length) {
      issues.push(
        notice("warn", [
          t("indexDangling", { n: idx.dangling.length }),
          h("ul", { class: "kv-list" }, idx.dangling.map(function (f) { return h("li", null, f); })),
        ])
      );
    }
    if (!issues.length) {
      issues.push(notice("info", t("indexHealthy")));
    }
    const health = h(
      "section",
      { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", null, t("indexHealth")), rebuildBtn),
      h(
        "div",
        { class: "panel-body" },
        h(
          "div",
          { class: "stats" },
          meterStat(t("indexLines"), idx.lines + " / " + idx.maxLines, idx.lines / idx.maxLines),
          meterStat(t("indexSize"), fmtBytes(idx.size) + " / " + fmtBytes(idx.maxBytes), idx.size / idx.maxBytes)
        ),
        issues
      )
    );
    const saveBtn = h(
      "button",
      { class: "btn primary small", disabled: draft === idx.content, onclick: function () { saveIndex(saveBtn); } },
      icon("check"),
      t("save")
    );
    const textarea = h("textarea", {
      class: "textarea",
      style: "min-height:380px",
      value: draft,
      spellcheck: "false",
      oninput: function (event) {
        state.indexDraft = event.target.value;
        saveBtn.disabled = state.indexDraft === idx.content;
      },
    });
    const editor = h(
      "section",
      { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { class: "file-name" }, "MEMORY.md"), saveBtn),
      h("div", { class: "panel-body" }, h("p", { style: "margin:0 0 10px;color:var(--text-3);font-size:12.5px" }, t("indexEditorHint")), textarea)
    );
    return h("div", { class: "stack" }, health, editor);
  }

  async function saveIndex(button) {
    if (state.indexDraft === null) {
      return;
    }
    await withBusy(button, async function () {
      try {
        const res = await apiPost("index/save", Object.assign(scopeParams(), { content: state.indexDraft }));
        state.indexDraft = null;
        toast(res.message || t("saved"));
        await loadDetail();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  async function rebuildIndex(button) {
    if (state.indexDraft !== null && state.indexDraft !== state.detail.index.content) {
      if (!(await confirmDiscard())) {
        return;
      }
    }
    await withBusy(button, async function () {
      try {
        const res = await apiPost("index/rebuild", scopeParams());
        state.indexDraft = null;
        toast(res.message || t("saved"));
        await loadDetail();
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  // ------------------------------------------------------------ 压缩标签页

  async function loadCompression() {
    if (state.compressionLoading) {
      return;
    }
    const sel = state.selected;
    state.compressionLoading = true;
    let result;
    try {
      result = await apiGet("session/state", { key: sel.key });
    } catch (err) {
      result = { error: err.message };
    }
    if (!isSelected(sel)) {
      // 已切到别的会话：那边的加载标记归它自己管
      return;
    }
    state.compressionLoading = false;
    state.compression = result;
    if (state.tab === "compression") {
      renderTab();
    }
  }

  function renderCompressionTab() {
    const c = state.compression;
    if (!c) {
      loadCompression();
      return spinnerBlock();
    }
    if (c.error) {
      return errorBlock(c.error, function () {
        state.compression = null;
        renderTab();
      });
    }
    const now = c.now || Date.now() / 1000;
    const cooling = c.failUntil > now;
    const parts = c.lastParts || {};
    const total = PARTS.reduce(function (sum, key) { return sum + (parts[key] || 0); }, 0);
    const partsBody = [];
    if (total > 0) {
      partsBody.push(
        h(
          "div",
          { class: "bar" },
          PARTS.map(function (key) {
            return parts[key]
              ? h("span", {
                  style: "width:" + ((parts[key] / total) * 100).toFixed(2) + "%;background:var(--p-" + key + ")",
                  title: t("part_" + key) + " " + fmtNum(parts[key]),
                })
              : null;
          })
        ),
        h(
          "div",
          { class: "legend" },
          PARTS.map(function (key) {
            return h("span", null, h("i", { style: "background:var(--p-" + key + ")" }), t("part_" + key), h("b", null, fmtNum(parts[key] || 0)));
          })
        )
      );
    } else {
      partsBody.push(h("span", { style: "color:var(--text-3)" }, t("noEstimate")));
    }

    const resetBtn = h(
      "button",
      { class: "btn danger small", onclick: function () { resetCompression(resetBtn); } },
      icon("rotate"),
      t("resetCompression")
    );
    const refreshBtn = h(
      "button",
      {
        class: "btn small",
        onclick: function () {
          state.compression = null;
          renderTab();
        },
      },
      icon("refresh"),
      t("refresh")
    );
    return h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "actions", style: "justify-content:space-between;align-items:center" },
        h(
          "span",
          { class: "tag " + (c.live ? "ok" : ""), title: c.live ? t("liveHint") : t("diskHint") },
          c.live ? t("liveTag") : t("diskTag")
        ),
        h("div", { class: "actions" }, refreshBtn, resetBtn)
      ),
      h(
        "div",
        { class: "stats" },
        stat(t("watermark"), fmtNum(c.watermark), t("watermarkSub")),
        stat(t("summaryChars"), t("chars", { n: fmtNum((c.summary || "").length) }), t("summaryCharsSub", { time: relTime(c.compressedAt) })),
        stat(t("lastEstimate"), t("tokens", { n: fmtNum(c.lastEstimate) }), t("ratioSub", { ratio: Number(c.ratio || 1).toFixed(2) })),
        stat(t("lastActual"), c.lastActual > 0 ? t("tokens", { n: fmtNum(c.lastActual) }) : "—", t("lastActualSub")),
        stat(t("cacheHit"), c.cacheHitTokens >= 0 ? t("tokens", { n: fmtNum(c.cacheHitTokens) }) : t("notReported"), t("cacheHitSub")),
        stat(
          t("chain"),
          cooling ? t("chainCooling") : t("chainOk"),
          cooling ? t("chainCoolingSub", { left: fmtDuration(c.failUntil - now), n: c.failCount }) : t("chainOkSub")
        ),
        stat(t("extract"), t("extractTurns", { n: c.turnsSinceExtract || 0 }), t("extractSub", { time: relTime(c.extractedAt) })),
        stat(t("lastRequest"), relTime(c.lastRequestAt), fmtTime(c.lastRequestAt))
      ),
      h(
        "section",
        { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", null, t("partsTitle"))),
        h("div", { class: "panel-body" }, partsBody)
      ),
      h(
        "section",
        { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", null, t("summaryTitle"))),
        h("div", { class: "panel-body" }, c.summary ? h("pre", { class: "pre" }, c.summary) : emptyState("inbox", t("noSummary")))
      )
    );
  }

  async function resetCompression(button) {
    const ok = await confirmDialog({
      title: t("resetCompression"),
      message: t("resetMessage"),
      confirm: t("reset"),
      danger: true,
    });
    if (!ok) {
      return;
    }
    await withBusy(button, async function () {
      try {
        const res = await apiPost("session/reset", { key: state.selected.key });
        toast(res.message || t("saved"));
        state.compression = null;
        if (state.tab === "compression") {
          renderTab();
        }
      } catch (err) {
        toast(err.message, "error");
      }
    });
  }

  // ------------------------------------------------------------ 归档标签页

  async function loadArchives() {
    if (state.archivesLoading) {
      return;
    }
    const sel = state.selected;
    state.archivesLoading = true;
    let result;
    try {
      const res = await apiGet("archives", { key: sel.key });
      result = { items: res.archives || [] };
    } catch (err) {
      result = { error: err.message };
    }
    if (!isSelected(sel)) {
      return;
    }
    state.archivesLoading = false;
    state.archives = result;
    if (state.tab === "archives") {
      renderTab();
    }
  }

  async function openArchive(id) {
    const sel = state.selected;
    state.archiveView = { loading: true };
    renderTab();
    let view;
    try {
      view = await apiGet("archive", { key: sel.key, id: id });
    } catch (err) {
      toast(err.message, "error");
      view = null;
    }
    if (!isSelected(sel)) {
      return;
    }
    state.archiveView = view;
    if (state.tab === "archives") {
      renderTab();
    }
  }

  function renderArchiveTab() {
    if (state.archiveView) {
      return renderArchiveView();
    }
    const a = state.archives;
    if (!a) {
      loadArchives();
      return spinnerBlock();
    }
    if (a.error) {
      return errorBlock(a.error, function () {
        state.archives = null;
        renderTab();
      });
    }
    const panel = h(
      "section",
      { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", null, t("archivesTitle")), h("span", { style: "color:var(--text-3);font-size:12.5px" }, t("archivesHint")))
    );
    if (!a.items.length) {
      panel.append(emptyState("archive", t("noArchivesTitle"), t("noArchivesHint")));
      return panel;
    }
    a.items.forEach(function (item) {
      panel.append(
        h(
          "button",
          {
            class: "archive-item",
            onclick: function () {
              openArchive(item.id);
            },
          },
          h("div", { class: "avatar" }, icon("archive")),
          h(
            "div",
            { class: "grow" },
            h("div", { class: "session-name" }, fmtTime(item.createdAt)),
            h("div", { class: "session-sub" }, t("archiveMeta", { count: item.count, chars: fmtNum(item.summaryChars) }))
          ),
          h("span", { class: "file-age" }, relTime(item.createdAt))
        )
      );
    });
    return panel;
  }

  function messageBubble(m) {
    const role = m.role === "user" ? "user" : m.role === "assistant" ? "assistant" : "tool";
    const tools = m.tools && m.tools.length ? h("div", { class: "msg-role", style: "margin:6px 0 0" }, t("toolCalls", { names: m.tools.join(", ") })) : null;
    return h(
      "div",
      { class: "msg " + role },
      h("div", { class: "msg-role" }, t("role_" + role)),
      m.text || (tools ? null : t("emptyMessage")),
      tools
    );
  }

  function renderArchiveView() {
    const a = state.archiveView;
    if (a.loading) {
      return spinnerBlock();
    }
    const messages = a.messages || [];
    return h(
      "div",
      { class: "stack" },
      h(
        "div",
        { class: "actions", style: "align-items:center" },
        h(
          "button",
          {
            class: "btn small",
            onclick: function () {
              state.archiveView = null;
              renderTab();
            },
          },
          icon("back"),
          t("backToArchives")
        ),
        h("strong", null, fmtTime(a.created_at))
      ),
      h(
        "section",
        { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", null, t("archiveSummary"))),
        h("div", { class: "panel-body" }, h("pre", { class: "pre" }, a.summary || "—"))
      ),
      h(
        "section",
        { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", null, t("archiveMessages", { n: messages.length }))),
        h("div", { class: "panel-body messages" }, messages.map(messageBubble))
      )
    );
  }

  // ------------------------------------------------------------ 会话操作

  async function editRemark() {
    const s = state.detail.session || {};
    const sel = state.selected;
    const value = await promptDialog({
      title: t("remarkTitle"),
      message: t("remarkHint"),
      value: s.remark || "",
    });
    if (value === null || !isSelected(sel)) {
      return;
    }
    try {
      const res = await apiPost("session/remark", { key: sel.key, remark: value });
      toast(res.message || t("saved"));
      if (!isSelected(sel) || !state.detail) {
        await loadOverview(true);
        return;
      }
      state.detail.session = res;
      renderMain();
      await loadOverview(true);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function purgeSession() {
    const sel = state.selected;
    const ctrl = openDialog({ title: t("purgeTitle"), body: h("p", null, t("purgeMessage")) });
    ctrl.setFoot([
      h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")),
      h("button", { class: "btn danger", onclick: function () { ctrl.close("memories"); } }, t("purgeMemories")),
      h("button", { class: "btn danger", onclick: function () { ctrl.close("all"); } }, t("purgeAll")),
    ]);
    const target = await ctrl.done;
    if (!target || !isSelected(sel)) {
      return;
    }
    try {
      const res = await apiPost("session/purge", { key: sel.key, target: target });
      toast(res.message || t("deleted"));
      if (state.editor) {
        state.editor.dirty = false;
      }
      state.indexDraft = null;
      if (target === "all") {
        detailToken += 1;
        state.selected = null;
        resetDetailState();
        backToList();
        renderMain();
        await loadOverview(true);
        return;
      }
      resetDetailState();
      await Promise.all([loadDetail(), loadOverview(true)]);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function consolidateScope() {
    if (!(await confirmDiscard())) {
      return;
    }
    const sel = state.selected;
    const existing = new Set(state.detail.files.map(function (f) { return f.file; }));
    const ctrl = openDialog({ title: t("consolidateTitle"), wide: true });
    ctrl.setBody(
      h("div", { class: "empty" }, h("div", { class: "spinner" }), h("strong", null, t("consolidateRunning")), h("span", null, t("consolidateRunningHint")))
    );
    ctrl.setFoot(h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")));
    let res;
    try {
      res = await apiPost("consolidate/preview", { scope: sel.scope, key: sel.key || "" });
    } catch (err) {
      if (ctrl.isOpen()) {
        ctrl.setBody(notice("warn", err.message));
        ctrl.setFoot(h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("close")));
      }
      return;
    }
    if (!ctrl.isOpen()) {
      return;
    }
    const ops = res.operations || [];
    if (!ops.length) {
      ctrl.setBody(emptyState("check", t("consolidateNothingTitle"), t("consolidateNothingHint")));
      ctrl.setFoot(h("button", { class: "btn primary", onclick: function () { ctrl.close(null); } }, t("close")));
      return;
    }
    const checks = ops.map(function () { return true; });
    const counter = h("span", { class: "grow" });
    const applyBtn = h("button", { class: "btn primary" }, t("applySelected"));
    function updateCounter() {
      const n = checks.filter(Boolean).length;
      counter.textContent = t("selectedCount", { n: n, total: ops.length });
      applyBtn.disabled = n === 0;
    }
    const rows = ops.map(function (op, i) {
      const kind =
        op.op === "delete"
          ? ["danger", t("opDelete")]
          : existing.has(op.file)
            ? ["warn", t("opRewrite")]
            : ["ok", t("opCreate")];
      const box = h("input", {
        type: "checkbox",
        checked: true,
        onchange: function (event) {
          checks[i] = event.target.checked;
          updateCounter();
        },
      });
      return h(
        "div",
        { class: "op" },
        box,
        h(
          "div",
          { style: "min-width:0" },
          h(
            "label",
            {
              class: "op-head",
              onclick: function (event) {
                if (event.target !== box) {
                  box.click();
                  event.preventDefault();
                }
              },
            },
            h("span", { class: "tag " + kind[0] }, kind[1]),
            h("span", { class: "file-name", style: "flex:none" }, op.file),
            op.op === "write" ? typeTag(op.type) : null
          ),
          op.reason ? h("div", { class: "op-reason" }, op.reason) : null,
          op.op === "write"
            ? h(
                "details",
                null,
                h("summary", null, t("previewContent")),
                h("pre", { class: "pre" }, composeMemory(stem(op.file), op.description, op.type, op.content))
              )
            : null
        )
      );
    });
    ctrl.setBody([
      h("p", null, t("consolidateReview")),
      res.skipped && res.skipped.length ? notice("warn", t("consolidateSkipped", { n: res.skipped.length })) : null,
      h("div", null, rows),
    ]);
    applyBtn.addEventListener("click", async function () {
      const chosen = ops.filter(function (op, i) { return checks[i]; });
      ctrl.busy = true;
      applyBtn.disabled = true;
      try {
        const result = await apiPost("consolidate/apply", { scope: sel.scope, key: sel.key || "", operations: chosen });
        ctrl.busy = false;
        ctrl.close(true);
        toast(result.message || t("saved"));
        (result.results || []).forEach(function (item) {
          if (!item.ok) {
            toast(item.file + "：" + item.message, "error");
          }
        });
        if (isSelected(sel)) {
          state.editor = null;
          state.fileQuery = "";
          state.searchResults = null;
          await Promise.all([loadDetail(), loadOverview(true)]);
        }
      } catch (err) {
        ctrl.busy = false;
        applyBtn.disabled = false;
        toast(err.message, "error");
      }
    });
    ctrl.setFoot([counter, h("button", { class: "btn", onclick: function () { ctrl.close(null); } }, t("cancel")), applyBtn]);
    updateCounter();
  }

  // ------------------------------------------------------------ 启动

  function bindShortcuts() {
    document.addEventListener("keydown", function (event) {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s") {
        return;
      }
      if (document.querySelector(".overlay")) {
        return;
      }
      if (state.tab === "memories" && state.editor && !state.editor.loading) {
        event.preventDefault();
        saveFile(null);
      } else if (state.tab === "index" && state.indexDraft !== null) {
        event.preventDefault();
        saveIndex(null);
      }
    });
  }

  async function init() {
    const app = document.getElementById("app");
    if (!bridge) {
      app.replaceChildren(h("div", { class: "center-state" }, emptyState("warn", ZH.noBridge)));
      return;
    }
    const context = await bridge.ready();
    let locale = context && context.locale;
    buildShell();
    bindShortcuts();
    bridge.onContext(function (next) {
      if (next && next.locale && next.locale !== locale) {
        locale = next.locale;
        buildShell();
      }
    });
    await loadOverview(false);
  }

  init();
})();
