import React, { useEffect, useMemo, useState } from "react";
import { toast } from "react-toastify";
import AppShell from "./AppShell";
import styles from "../css/Reports.module.css";

const Reports = () => {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [metrics, setMetrics] = useState(null);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState("");
  const [testcasesByProject, setTestcasesByProject] = useState({});
  const [testcasesLoading, setTestcasesLoading] = useState(false);
  const [testcasesError, setTestcasesError] = useState("");

  const apiBase = process.env.REACT_APP_API_URL || "http://127.0.0.1:8001";
  const activeProjectName = localStorage.getItem("activeProjectName") || "";

  useEffect(() => {
    const token = localStorage.getItem("token");
    if (!token) {
      return;
    }

    const loadProjects = async () => {
      setLoading(true);
      setError("");
      try {
        const res = await fetch(`${apiBase}/projects`, {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = await res.json();
        setProjects(Array.isArray(data?.projects) ? data.projects : []);
      } catch (err) {
        const msg = err?.message || "Failed to load projects.";
        setError(msg);
        setProjects([]);
      } finally {
        setLoading(false);
      }
    };

    loadProjects();
  }, []);

  useEffect(() => {
    if (!projects.length) {
      return;
    }
    const token = localStorage.getItem("token");
    if (!token) {
      return;
    }
    const loadTestcases = async () => {
      setTestcasesLoading(true);
      setTestcasesError("");
      try {
        const results = await Promise.all(
          projects.map(async (project) => {
            const res = await fetch(`${apiBase}/projects/${project.id}/testcases`, {
              headers: {
                Authorization: `Bearer ${token}`,
              },
            });
            if (!res.ok) {
              throw new Error(`HTTP ${res.status}`);
            }
            const data = await res.json();
            return [project.id, data];
          })
        );
        const mapped = {};
        results.forEach(([id, data]) => {
          mapped[id] = data;
        });
        setTestcasesByProject(mapped);
      } catch (err) {
        setTestcasesError(err?.message || "Failed to load testcases.");
        setTestcasesByProject({});
      } finally {
        setTestcasesLoading(false);
      }
    };

    loadTestcases();
  }, [projects]);

  useEffect(() => {
    const loadMetrics = async () => {
      setMetricsLoading(true);
      setMetricsError("");
      try {
        const res = await fetch(`${apiBase}/metrics/dashboard`);
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = await res.json();
        setMetrics(data || null);
      } catch (err) {
        setMetricsError(err?.message || "Failed to load analytics.");
        setMetrics(null);
      } finally {
        setMetricsLoading(false);
      }
    };

    loadMetrics();
  }, []);

  const sortedProjects = useMemo(() => {
    return [...projects].sort((a, b) => {
      const aDate = a?.created_at ? new Date(a.created_at).getTime() : 0;
      const bDate = b?.created_at ? new Date(b.created_at).getTime() : 0;
      return bDate - aDate;
    });
  }, [projects]);

  const latestRun = metrics?.latest_run || {};
  const statusCounts = latestRun?.status_counts || {};
  const totalTests =
    Number.isFinite(latestRun?.total) && latestRun.total !== null
      ? latestRun.total
      : Object.values(statusCounts).reduce((sum, val) => sum + (val || 0), 0);
  const passedTests = statusCounts?.passed ?? statusCounts?.pass ?? 0;
  const failedTests = statusCounts?.failed ?? statusCounts?.fail ?? 0;
  const passRate =
    latestRun?.pass_rate !== undefined && latestRun?.pass_rate !== null
      ? Math.round(latestRun.pass_rate * 100)
      : totalTests
        ? Math.round((passedTests / totalTests) * 100)
        : 0;

  const openReport = (project) => {
    const rawId = project?.id ?? project?.project_id ?? project?.projectId ?? null;
    const numericId = rawId !== null ? Number(rawId) : NaN;
    if (!Number.isInteger(numericId)) {
      toast.error("Project id is missing for this report.");
      return;
    }
    const reportUrl = `${apiBase}/reports/view/${numericId}/`;
    window.open(reportUrl, "_blank", "noopener,noreferrer");
  };

  return (
    <AppShell
      title="Reports"
      subtitle="Insights"
      contextItems={[
        { label: "Active Project", value: activeProjectName || "Not selected" },
      ]}
    >
      <div className={styles.reportsContainer}>
        <section className={styles.reportsPanel}>
          <div className={styles.reportsHeader}>
            <div>
              <p className={styles.reportsOverline}>All Projects</p>
              <h2 className={styles.reportsTitle}>Execution Reports</h2>
            </div>
            <span className={styles.reportsHint}>Open the latest report per project.</span>
          </div>

          <div className={styles.analyticsGrid}>
            <div className={styles.analyticsCard}>
              <span>Total Testcases</span>
              <strong>{metricsLoading ? "…" : totalTests || 0}</strong>
              <p>Latest run coverage</p>
            </div>
            <div className={styles.analyticsCard}>
              <span>Passed</span>
              <strong>{metricsLoading ? "…" : passedTests || 0}</strong>
              <p>Successful validations</p>
            </div>
            <div className={styles.analyticsCard}>
              <span>Failed</span>
              <strong>{metricsLoading ? "…" : failedTests || 0}</strong>
              <p>Requires attention</p>
            </div>
            <div className={styles.analyticsCard}>
              <span>Pass Rate</span>
              <strong>{metricsLoading ? "…" : `${passRate}%`}</strong>
              <p>Latest run performance</p>
            </div>
          </div>

          {metricsError && <div className={styles.errorBanner}>{metricsError}</div>}

          {loading && (
            <div className={styles.stateBanner}>
              <span className="globalSpinner" aria-hidden="true"></span>
              Loading reports...
            </div>
          )}
          {testcasesLoading && (
            <div className={styles.stateBanner}>
              <span className="globalSpinner" aria-hidden="true"></span>
              Loading testcases...
            </div>
          )}
          {testcasesError && <div className={styles.errorBanner}>{testcasesError}</div>}
          {error && <div className={styles.errorBanner}>{error}</div>}

          {!loading && !error && sortedProjects.length === 0 && (
            <div className={styles.emptyState}>
              <h3>No projects yet</h3>
              <p>Create a project to generate and view reports.</p>
            </div>
          )}

          {!loading && !error && sortedProjects.length > 0 && (
            <div className={styles.reportsTable}>
              <div className={styles.reportsHeaderRow}>
                <span>Project</span>
                <span>Framework</span>
                <span>Created</span>
                <span>Testcases</span>
                <span>Report</span>
              </div>
              {sortedProjects.map((project) => (
                <div key={project.id || project.project_name} className={styles.reportsRow}>
                  <div className={styles.reportsCell}>
                    <div className={styles.projectName}>{project.project_name}</div>
                    <div className={styles.projectMeta}>Saved</div>
                  </div>
                  <div className={styles.reportsCell}>
                    {(project.framework || "").trim()} {project.language ? ` / ${project.language}` : ""}
                  </div>
                  <div className={styles.reportsCell}>
                    {project.created_at ? new Date(project.created_at).toLocaleString() : "-"}
                  </div>
                  <div className={styles.reportsCell}>
                    {testcasesByProject[project.id]?.tests?.length ? (
                      <div className={styles.testcaseCount}>
                        {testcasesByProject[project.id].tests.length} testcases
                      </div>
                    ) : (
                      <span className={styles.testcaseEmpty}>
                        {testcasesLoading ? "Loading..." : "No testcases"}
                      </span>
                    )}
                  </div>
                  <div className={styles.reportsCell}>
                    <button
                      type="button"
                      className={styles.reportButton}
                      onClick={() => openReport(project)}
                    >
                      Open Report
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </AppShell>
  );
};

export default Reports;
