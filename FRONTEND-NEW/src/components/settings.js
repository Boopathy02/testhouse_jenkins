import React, { useEffect, useState } from "react";
import AppShell from "./AppShell";
import styles from "../css/Settings.module.css";

const Settings = () => {
  const [profile, setProfile] = useState({ name: "", email: "", organization: "" });
  const [apiKey, setApiKey] = useState("");
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    const token = localStorage.getItem("token");
    if (token) {
      try {
        const payload = JSON.parse(atob(token.split(".")[1]));
        setProfile({
          name: payload.name || "",
          email: payload.sub || "",
          organization: payload.org || "",
        });
      } catch (e) {
        setProfile({ name: "", email: "", organization: "" });
      }
    }
    const storedKey = localStorage.getItem("OPENAI_API_KEY") || "";
    setApiKey(storedKey);
  }, []);

  const handleSave = () => {
    localStorage.setItem("OPENAI_API_KEY", apiKey);
    setSaved(true);
    setTimeout(() => setSaved(false), 1500);
  };

  return (
    <AppShell title="Settings" subtitle="Profile & API">
      <div className={styles.settingsContainer}>
        <section className={styles.settingsPanel}>
          <div className={styles.settingsHeader}>
            <div>
              <p className={styles.settingsOverline}>Account</p>
              <h2 className={styles.settingsTitle}>Profile</h2>
            </div>
            <span className={styles.settingsHint}>Manage your workspace preferences.</span>
          </div>

          <div className={styles.profileGrid}>
            <div className={styles.profileCard}>
              <label className={styles.fieldLabel}>Name</label>
              <input
                type="text"
                value={profile.name}
                placeholder="Your name"
                onChange={(e) => setProfile((prev) => ({ ...prev, name: e.target.value }))}
              />
            </div>
            <div className={styles.profileCard}>
              <label className={styles.fieldLabel}>Email</label>
              <input type="email" value={profile.email} readOnly />
            </div>
            <div className={styles.profileCard}>
              <label className={styles.fieldLabel}>Organization</label>
              <input type="text" value={profile.organization} readOnly />
            </div>
          </div>

          <div className={styles.apiPanel}>
            <div>
              <h3>OpenAI API Key</h3>
              <p>Store your API key securely on this device for future use.</p>
            </div>
            <div className={styles.apiForm}>
              <input
                type="password"
                value={apiKey}
                placeholder="sk-..."
                onChange={(e) => setApiKey(e.target.value)}
              />
              <button type="button" className={styles.saveButton} onClick={handleSave}>
                {saved ? "Saved" : "Save"}
              </button>
            </div>
          </div>
        </section>
      </div>
    </AppShell>
  );
};

export default Settings;
