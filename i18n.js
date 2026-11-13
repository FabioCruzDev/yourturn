// Idioma da tela (Configurações → Idioma). O texto-fonte é pt-BR; i18n_en.json (servido em /i18n.js como I18N_EN) traz
// {"texto pt": "texto en"}. t("Olá, {nome}", {nome}) traduz e depois preenche os {campos}.
// O idioma fica no settings.json do servidor (vale em todos os aparelhos); o localStorage só guarda para a 1ª pintura.
window.LANG = (() => { try { return localStorage.getItem("lang"); } catch { return null; } })()
  || ((navigator.language || "").toLowerCase().startsWith("pt") ? "pt-BR" : "en-US");
window.t = (s, vars) => {
  let r = window.LANG === "en-US" && window.I18N_EN && window.I18N_EN[s] != null ? window.I18N_EN[s] : s;
  return vars ? r.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m)) : r;
};
// Traduz o HTML estático: textos e os atributos title/placeholder/aria-label (inclusive dentro de <template>).
window.translateDom = (root = document) => {
  if (window.LANG !== "en-US") return;
  const walk = (node) => {
    const it = document.createTreeWalker(node, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = it.currentNode; n; n = it.nextNode()) {
      if (n.nodeType === 3) {
        const k = n.nodeValue.trim();
        if (k && window.I18N_EN[k] != null && !/^(SCRIPT|STYLE)$/.test(n.parentNode?.nodeName)) n.nodeValue = n.nodeValue.replace(k, window.I18N_EN[k]);
      } else {
        for (const a of ["title", "placeholder", "aria-label"]) {
          const v = n.getAttribute?.(a);
          if (v && window.I18N_EN[v] != null) n.setAttribute(a, window.I18N_EN[v]);
        }
        if (n.nodeName === "TEMPLATE") walk(n.content);
      }
    }
  };
  walk(root.body || root);
  document.documentElement.lang = "en";
  if (window.I18N_EN[document.title] != null) document.title = window.I18N_EN[document.title];
};
// O servidor manda o idioma salvo: se for outro, guarda e recarrega; se ainda não tiver nenhum, grava o deste navegador.
window.syncLang = (settings, save) => {
  if (!settings) return;
  if (!settings.lang) return save(window.LANG);
  if (settings.lang !== window.LANG) {
    let saved = false;
    try { localStorage.setItem("lang", settings.lang); saved = localStorage.getItem("lang") === settings.lang; } catch { /* sem localStorage */ }
    if (saved) location.reload(); // sem localStorage, recarregar entraria em loop: fica no idioma do navegador
  }
};
