import React, { useState } from "react";
import AppShell from "./AppShell";
import styles from "../css/TestRunner.module.css";

const API_BASE = "http://localhost:8001";

export default function TestRunner() {
  const [loading, setLoading] = useState(false);
  const [reportLoading, setReportLoading] = useState(false);
  const [error, setError] = useState(null);

  const runTests = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/tests/run`, { method: "GET" });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const data = await res.json();
      if (!data.report_url) throw new Error("No report_url returned");
      const url = data.report_url.startsWith("http") ? data.report_url : `${API_BASE}${data.report_url}`;
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      setError(e.message || "Failed to run tests");
    } finally {
      setLoading(false);
    }
  };

  const viewReport = async () => {
    setReportLoading(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/tests/report?test=tests/test_1.py`, { method: "GET" });
      if (!res.ok) throw new Error(`Request failed: ${res.status}`);
      const data = await res.json();
      if (!data.report_url) throw new Error("No report_url returned");
      const url = data.report_url.startsWith("http") ? data.report_url : `${API_BASE}${data.report_url}`;
      window.open(url, "_blank", "noopener,noreferrer");
    } catch (e) {
      setError(e.message || "Failed to fetch report");
    } finally {
      setReportLoading(false);
    }
  };

  return (
    <AppShell
      title="Test Runner"
      subtitle="Execution Controls"
      contextItems={[{ label: "Project", value: localStorage.getItem("activeProjectName") || "Unknown" }]}
    >
      <div className={styles.runnerPanel}>
        <div className={styles.runnerHeader}>
          <div>
            <h2>Automated Regression</h2>
            <p>Trigger test execution and open the latest report.</p>
          </div>
          <div className={styles.runnerMeta}>
            <span>Environment</span>
            <strong>Local Backend</strong>
          </div>
        </div>
        <div className={styles.runnerActions}>
          <button onClick={viewReport} disabled={reportLoading} className={styles.secondaryButton}>
            {reportLoading ? (
              <>
                <span className="globalSpinner" aria-hidden="true"></span>
                Preparing report...
              </>
            ) : (
              "View Report"
            )}
          </button>
          <button onClick={runTests} disabled={loading} className={styles.primaryButton}>
            {loading ? (
              <>
                <span className="globalSpinner" aria-hidden="true"></span>
                Running tests...
              </>
            ) : (
              "Run Tests & Open Report"
            )}
          </button>
        </div>
        {error && <div className={styles.errorBanner}>{error}</div>}
      </div>
    </AppShell>
  );
}
