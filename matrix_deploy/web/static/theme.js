// Loaded in <head> so the saved theme applies before first paint (an external
// file because the Content-Security-Policy allows no inline scripts).
document.documentElement.setAttribute("data-theme", localStorage.getItem("md-theme") || "light");
