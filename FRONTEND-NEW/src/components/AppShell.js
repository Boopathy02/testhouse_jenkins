import React, { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import styles from "../css/AppShell.module.css";

const AppShell = ({
  title,
  subtitle,
  contextItems = [],
  actions,
  children,
  compact = false,
}) => {
  const [isLightTheme, setIsLightTheme] = useState(false);

  useEffect(() => {
    const saved = localStorage.getItem("uiTheme");
    if (saved === "light") {
      setIsLightTheme(true);
    }
  }, []);

  useEffect(() => {
    const theme = isLightTheme ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("uiTheme", theme);
  }, [isLightTheme]);

  const navItems = [
    { label: "Dashboard", to: "/" },
    { label: "Projects", to: "/projects" },
    { label: "Reports", to: "/reports" },
    { label: "Settings", to: "/settings" },
  ];

  return (
    <div className={`${styles.shell} ${compact ? styles.shellCompact : ""}`}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <div className={styles.brandMark}>
            <i className="fa-solid fa-code"></i>
          </div>
          <div>
            <div className={styles.brandTitle}>Boopathy</div>
            <div className={styles.brandSubtitle}>AI Test Automation</div>
          </div>
        </div>
        <div className={styles.navSection}>
          <div className={styles.navLabel}>Workspace</div>
          <nav className={styles.navList}>
            {navItems
              .filter((item) => item.label !== "Settings")
              .map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `${styles.navItem} ${isActive ? styles.navItemActive : ""}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
          </nav>
        </div>
        <div style={{ marginTop: "auto" }}>
          <nav className={styles.navList}>
            {navItems
              .filter((item) => item.label === "Settings")
              .map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `${styles.navItem} ${isActive ? styles.navItemActive : ""}`
                  }
                >
                  {item.label}
                </NavLink>
              ))}
          </nav>
        </div>
      </aside>
      <div className={styles.main}>
        <header className={styles.topbar}>
          <div className={styles.pageIntro}>
            {subtitle ? <div className={styles.pageOverline}>{subtitle}</div> : null}
            <h1 className={styles.pageTitle}>{title}</h1>
          </div>
          <div className={styles.topbarMeta}>
            {contextItems
              .filter((item) => item && item.value)
              .map((item) => (
                <div key={item.label} className={styles.metaItem}>
                  <span>{item.label}</span>
                  <strong>{item.value}</strong>
                </div>
              ))}
          </div>
          <div className={styles.topbarActions}>
            {actions}
            <button
              type="button"
              className={styles.themeToggle}
              onClick={() => setIsLightTheme((prev) => !prev)}
              aria-label="Toggle light theme"
            >
              <i className={`fa-solid ${isLightTheme ? "fa-moon" : "fa-sun"}`}></i>
            </button>
          </div>
        </header>
        <div className={styles.content}>{children}</div>
      </div>
    </div>
  );
};

export default AppShell;
