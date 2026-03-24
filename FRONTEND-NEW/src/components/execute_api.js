import React, { useMemo, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import axios from "axios";
import API_BASE_URL from "../config";
import { toast } from "react-toastify";
import styles from "../css/Execute.module.css";


const QualitySparkline = ({ data = [] }) => {
  const points = useMemo(() => {
    if (!data.length) return "";
    const values = data.map((item) => item.pass_rate ?? 0);
    const chartValues = values.length === 1 ? [values[0], values[0]] : values;
    const max = Math.max(...chartValues);
    const min = Math.min(...chartValues);
    const span = max - min || 1;
    return chartValues
      .map((value, index) => {
        const x = (index / (chartValues.length - 1 || 1)) * 100;
        const y = 100 - ((value - min) / span) * 100;
        return `${x},${y}`;
      })
      .join(" ");
  }, [data]);

  if (!points) {
    return <div className={styles.metricsSparklinePlaceholder}>No data</div>;
  }

  return (
    <svg viewBox="0 0 100 100" className={styles.metricsSparkline}>
      <polyline
        points={points}
        fill="none"
        stroke="var(--color-primary)"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
};

const Execute = ({ onBack, fullTestData, projectName,
  projectId, }) => {
  const navigate = useNavigate();
  const [loadingExecution, setLoadingExecution] = useState(false);
  const [runningTestName, setRunningTestName] = useState("");
  const [executionSuccess, setExecutionSuccess] = useState(false);
  const [executionError, setExecutionError] = useState(false);
  const [error, setError] = useState("");
  const [acResults, setAcResults] = useState(null);

  const [reportLoading, setReportLoading] = useState(false);
  const [visualizerImages, setVisualizerImages] = useState([]);
  const [visualizerLoading, setVisualizerLoading] = useState(false);
  const [visualizerError, setVisualizerError] = useState("");
  const [showVisualizer, setShowVisualizer] = useState(false);
  const [visualizerMode, setVisualizerMode] = useState("idle"); // idle | interactive | images
  const [visualizerDashboardUrl, setVisualizerDashboardUrl] = useState("");

  const [metrics, setMetrics] = useState(null);
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsError, setMetricsError] = useState("");
  const [plannedTests, setPlannedTests] = useState([]);
  const [plannedTestsProjectId, setPlannedTestsProjectId] = useState(null);
  const [plannedTestsProjectName, setPlannedTestsProjectName] = useState("");

  const [executionFilters, setExecutionFilters] = useState({
    ui: { regression: false, functional: false },
    accessibility: { regression: false, functional: false },
    security: { regression: false, functional: false },
  });
  const [categorySelections, setCategorySelections] = useState({
    accessibility: false,
    security: false,
  });
  const [useUpdatedExecutionByCategory, setUseUpdatedExecutionByCategory] = useState({
    ui: false,
    accessibility: false,
    security: false,
  });
  const [tagCounts, setTagCounts] = useState({});
  const [filtersExpanded, setFiltersExpanded] = useState({
    ui: true,
    accessibility: false,
    security: false,
  });
  const [testPageIndex, setTestPageIndex] = useState(1);
  const testsPerPage = 8;

  const hasSelectedTags = useMemo(() => {
    const uiSelected = Object.values(executionFilters.ui).some(Boolean);
    return uiSelected || categorySelections.accessibility || categorySelections.security;
  }, [executionFilters, categorySelections]);

  const hasUpdatedSelection = useMemo(
    () => Object.values(useUpdatedExecutionByCategory).some(Boolean),
    [useUpdatedExecutionByCategory]
  );

  const [activeProjectId, setActiveProjectId] = useState(() => projectId || localStorage.getItem("projectId"));

  useEffect(() => {
    if (projectId) {
      const id = String(projectId);
      setActiveProjectId(id);
      localStorage.setItem("projectId", id);
    }
    if (projectName) {
      localStorage.setItem("activeProjectName", projectName);
    }
  }, [projectId, projectName]);

  const getStorageKey = (suffix) => {
    const id = projectId || localStorage.getItem("projectId") || "default";
    return `testify:${id}:execute:${suffix}`;
  };

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(getStorageKey("state"));
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed.plannedTests)) {
          setPlannedTests(parsed.plannedTests);
        }
        if (parsed.acResults) {
          setAcResults(parsed.acResults);
        }
        if (typeof parsed.error === "string") {
          setError(parsed.error);
        }
        if (parsed.tagCounts) {
          setTagCounts(parsed.tagCounts);
        }
        if (parsed.executionFilters) {
          setExecutionFilters(parsed.executionFilters);
        }
        if (parsed.categorySelections) {
          setCategorySelections(parsed.categorySelections);
        }
        if (parsed.useUpdatedExecutionByCategory) {
          setUseUpdatedExecutionByCategory(parsed.useUpdatedExecutionByCategory);
        }
        if (typeof parsed.testPageIndex === "number") {
          setTestPageIndex(parsed.testPageIndex);
        }
      }
    } catch (err) {
      console.warn("Failed to restore execute state:", err);
    }
  }, []);

  useEffect(() => {
    try {
      const payload = {
        plannedTests,
        acResults,
        error,
        tagCounts,
        executionFilters,
        categorySelections,
        useUpdatedExecutionByCategory,
        testPageIndex,
      };
      sessionStorage.setItem(getStorageKey("state"), JSON.stringify(payload));
    } catch (err) {
      console.warn("Failed to persist execute state:", err);
    }
  }, [
    plannedTests,
    acResults,
    error,
    tagCounts,
    executionFilters,
    categorySelections,
    useUpdatedExecutionByCategory,
    testPageIndex,
  ]);


  const ensureActiveProject = useCallback(async () => {
    if (activeProjectId) {
      return activeProjectId;
    }
    const token = localStorage.getItem("token");
    const name = projectName || localStorage.getItem("activeProjectName");
    if (!token || !name) {
      return null;
    }
    try {
      const response = await fetch(`${API_BASE_URL}/projects/activate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ project_name: name }),
      });
      if (!response.ok) {
        const txt = await response.text().catch(() => null);
        throw new Error(txt || `Failed to activate project (${response.status})`);
      }
      const payload = await response.json();
      const newId = payload?.project?.id ? String(payload.project.id) : null;
      if (newId) {
        setActiveProjectId(newId);
        localStorage.setItem("projectId", newId);
      }
      return newId;
    } catch (err) {
      console.error("Failed to activate project before upload:", err);
      toast.error(err.message || "Failed to activate project.");
      return null;
    }
  }, [activeProjectId, projectName]);

  const formatServerError = (err, fallbackMessage) => {
    const detail =
      err?.response?.data?.detail ??
      err?.response?.data?.message ??
      err?.response?.data ??
      err?.message;
    if (typeof detail === "object") {
      try {
        return JSON.stringify(detail);
      } catch {
        return fallbackMessage;
      }
    }
    return detail || fallbackMessage;
  };

  useEffect(() => {
    setPlannedTests([]);
    setPlannedTestsProjectId(null);
    setPlannedTestsProjectName("");
    setTagCounts({});
    setTestPageIndex(1);
  }, []);

  const executeStoryTest = async () => {
    await executeApiFeature();
  };

  const executeApiFeature = async () => {
    setLoadingExecution(true);
    setError("");
    setExecutionSuccess(false);
    setExecutionError(false);
    try {
      const activeId = await ensureActiveProject();
      if (!activeId) {
        throw new Error("No active project. Please start a project first.");
      }
      const token = localStorage.getItem("token");
      const res = await axios.post(`${API_BASE_URL}/tests/api/run-feature?project_id=${activeId}`, null, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res?.data?.status === "ok") {
        toast.success("API feature executed successfully.");
        setExecutionSuccess(true);
      } else {
        throw new Error("Unexpected response from server.");
      }
    } catch (err) {
      const formattedError = formatServerError(err, "Error executing the API feature.");
      setError(formattedError);
      setExecutionError(true);
      toast.error(formattedError);
    } finally {
      setLoadingExecution(false);
    }
  };

  const viewReport = async () => {
    setReportLoading(true);
    setError("");
    setPlannedTests([]);
    const activeId = await ensureActiveProject();
    if (!activeId) {
      throw new Error("No active project. Please start a project first.");
    }
    try {
      const reportUrl = `${API_BASE_URL}/reports/view/${activeId}/`;
      window.open(reportUrl, "_blank", "noopener,noreferrer");
    } catch (err) {
      setError(formatServerError(err, "Failed to open report"));
    } finally {
      setReportLoading(false);
    }
  };

  const formatVisualizerAssetUrl = (url) => {
    if (!url) return "";
    if (/^https?:\/\//i.test(url)) {
      return url;
    }
    const base = API_BASE_URL.endsWith("/") ? API_BASE_URL.slice(0, -1) : API_BASE_URL;
    const path = url.startsWith("/") ? url : `/${url}`;
    return `${base}${path}`;
  };

  const loadVisualizerContent = async () => {
    if (visualizerLoading) {
      return;
    }
    setVisualizerLoading(true);
    setVisualizerError("");
    try {
      const res = await axios.get(`${API_BASE_URL}/visualizer/images`);
      const images = Array.isArray(res.data?.images) ? res.data.images : [];
      const dashboardPath = res.data?.interactive_dashboard;

      if (dashboardPath) {
        const absoluteDashboard = formatVisualizerAssetUrl(dashboardPath);
        setVisualizerDashboardUrl(`${absoluteDashboard}?t=${Date.now()}`);
        setVisualizerMode("interactive");
        setVisualizerImages([]);
      } else {
        setVisualizerImages(images);
        setVisualizerMode("images");
      }

      setShowVisualizer(true);
    } catch (err) {
      setVisualizerError(formatServerError(err, "Failed to load visualizations."));
    } finally {
      setVisualizerLoading(false);
    }
  };

  const closeVisualizer = () => {
    setShowVisualizer(false);
    setVisualizerError("");
  };

  const handleToggleVisualizer = async () => {
    if (visualizerLoading) {
      return;
    }
    if (showVisualizer) {
      closeVisualizer();
      return;
    }

    if (visualizerMode === "idle") {
      await loadVisualizerContent();
      return;
    }

    setShowVisualizer(true);
  };

  const handleRefreshVisualizer = async () => {
    if (visualizerLoading) {
      return;
    }
    if (visualizerMode === "interactive" && visualizerDashboardUrl) {
      const baseUrl = visualizerDashboardUrl.split("?")[0];
      setVisualizerDashboardUrl(`${baseUrl}?t=${Date.now()}`);
      return;
    }
    await loadVisualizerContent();
  };

  const formatPercent = (value) => `${Math.round((value ?? 0) * 100)}%`;
  const formatCount = (value) => (value ?? 0);
  const summary = metrics?.self_healing_summary || {};
  const periods = metrics?.periods || {};
  const latestStatus = metrics?.latest_run?.status_counts || {};
  const selfHealing = metrics?.self_healing_reports || {};
  const healingStrategies = selfHealing.strategy_usage || [];
  const healingSteps = selfHealing.healing_steps_per_feature || [];
  const healingHistory = selfHealing.history || [];

  const fetchMetrics = async () => {
    setMetricsLoading(true);
    setMetricsError("");
    try {
      const res = await axios.get(`${API_BASE_URL}/metrics/dashboard`);
      setMetrics(res.data);
    } catch (err) {
      setMetricsError(formatServerError(err, "Unable to load quality metrics."));
    } finally {
      setMetricsLoading(false);
    }
  };

  useEffect(() => {
    fetchMetrics();
  }, []);

  const toggleExecutionTag = (category, tag) => {
    setExecutionFilters((prev) => ({
      ...prev,
      [category]: {
        ...prev[category],
        [tag]: !prev[category][tag],
      },
    }));
  };

  const toggleFilterSection = (category) => {
    setFiltersExpanded((prev) => ({
      ...prev,
      [category]: !prev[category],
    }));
  };

  const toggleUpdatedExecution = (category) => {
    setUseUpdatedExecutionByCategory((prev) => ({
      ...prev,
      [category]: !prev[category],
    }));
  };

  const handleOpenTestFile = (filePath) => {
    if (!plannedTestsProjectId || !filePath) {
      return;
    }
    navigate("/editor", {
      state: {
        openFile: {
          projectId: plannedTestsProjectId,
          projectName: plannedTestsProjectName,
          path: filePath,
        },
      },
    });
  };

  const flatPlannedTests = useMemo(() => {
    if (!plannedTests.length) {
      return [];
    }
    const flattened = [];
    plannedTests.forEach((plan) => {
      const tests = plan.tests || [];
      const category = plan.category || "ui";
      const scriptKey = plan.script || plan.script_path || plan.name || "";
      tests.forEach((testName) => {
        const filePath =
          plan.test_files?.find((item) => item.name === testName)?.path || plan.script_path;
        flattened.push({
          category,
          scriptKey,
          script_path: plan.script_path,
          test_files: plan.test_files,
          testName,
          filePath,
        });
      });
    });
    return flattened;
  }, [plannedTests]);

  const totalPlannedCount = flatPlannedTests.length;
  const totalPages = Math.max(1, Math.ceil(totalPlannedCount / testsPerPage));
  const pagedPlans = useMemo(() => {
    if (!flatPlannedTests.length) {
      return [];
    }
    const start = (testPageIndex - 1) * testsPerPage;
    const pageItems = flatPlannedTests.slice(start, start + testsPerPage);
    const grouped = new Map();
    pageItems.forEach((item) => {
      const key = `${item.category}:${item.scriptKey}`;
      if (!grouped.has(key)) {
        grouped.set(key, {
          category: item.category,
          script: item.scriptKey,
          script_path: item.script_path,
          test_files: item.test_files,
          tests: [],
        });
      }
      grouped.get(key).tests.push({ name: item.testName, path: item.filePath });
    });
    return Array.from(grouped.values());
  }, [flatPlannedTests, testPageIndex, testsPerPage]);

  return (
    <div className={styles.executeContainer}>
      <div className={styles.contentBox}>
        {/* Heading Section */}
        <h3 className={styles.heading}>
          <i className={`fa-solid fa-code ${styles.headingIcon}`}></i>
          Generate Scripts
        </h3>
        <p className={styles.subheading}>Configure framework and generate test scripts</p>

        {/* Icon & Description */}
        <div className={styles.centerContent}>
          <div className={styles.mainIcon}>
            <i className="fa-solid fa-code"></i>
          </div>
          <h2 className={styles.mainTitle}>Generate Test Scripts</h2>
          <p className={styles.mainDescription}>
            Your test scripts will be generated based on the uploaded designs and user stories.
          </p>
        </div>


        {/* Action Buttons: Report + Execute + Visualize */}
        <div className={styles.executeButtonContainer}>
          <div className={styles.actionButtons}>
            <button onClick={viewReport} disabled={reportLoading} className={styles.reportButton}>
              {reportLoading ? (
                <>
                  <span className="globalSpinner" aria-hidden="true"></span>
                  Opening report...
                </>
              ) : (
                "Report"
              )}
            </button>

            <button onClick={executeApiFeature} disabled={loadingExecution} className={styles.executeButton}>
              {loadingExecution ? (
                <>
                  <span className="globalSpinner" aria-hidden="true"></span>
                  Executing...
                </>
              ) : (
                "Execute"
              )}
            </button>

          </div>
          {error && <div className={styles.errorLog}>{error}</div>}
          {acResults && (
            <div className={styles.acResultsCard}>
              <div className={styles.acResultsHeader}>
                <h4>Acceptance Criteria</h4>
                <span>{acResults.overall_status || "UNKNOWN"}</span>
              </div>
              {Array.isArray(acResults.details) && acResults.details.length ? (
                <ul className={styles.acResultsList}>
                  {acResults.details.map((item, idx) => (
                    <li key={`${idx}-${item.ac}`}>
                      <strong>{item.status}</strong> {item.ac}
                      {item.reason ? <span className={styles.acResultsReason}> - {item.reason}</span> : null}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className={styles.acResultsEmpty}>No acceptance criteria detected.</p>
              )}
            </div>
          )}
          {showVisualizer && (
            <div className={styles.visualizerPanel}>
              <div className={styles.visualizerPanelHeader}>
                <h4>Allure Visualizations</h4>
                <button type="button" onClick={closeVisualizer} className={styles.visualizerPanelClose}>
                  Close
                </button>
              </div>
              {visualizerLoading && (
                <p className={styles.visualizerStatus}>
                  <span className="globalSpinner" aria-hidden="true"></span>
                  Fetching the latest visualizations...
                </p>
              )}
              {visualizerError && <p className={styles.visualizerError}>{visualizerError}</p>}
              {!visualizerLoading && !visualizerError && (
                <div className={styles.visualizerToolbar}>
                  <span>
                    {visualizerMode === "interactive"
                      ? "Interactive Plotly dashboard"
                      : visualizerImages.length
                        ? "Static chart snapshots"
                        : "No visualizations detected yet"}
                  </span>
                  <div className={styles.visualizerToolbarActions}>
                    <button type="button" onClick={handleRefreshVisualizer}>
                      Refresh
                    </button>
                    {visualizerMode === "interactive" && visualizerDashboardUrl && (
                      <a
                        href={visualizerDashboardUrl}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        Open full screen
                      </a>
                    )}
                  </div>
                </div>
              )}
              {!visualizerLoading && !visualizerError && (
                visualizerMode === "interactive" && visualizerDashboardUrl ? (
                  <div className={styles.visualizerIframeWrapper}>
                    <iframe
                      key={visualizerDashboardUrl}
                      src={visualizerDashboardUrl}
                      title="Interactive Allure dashboard"
                      className={styles.visualizerIframe}
                      loading="lazy"
                    />
                  </div>
                ) : visualizerImages.length ? (
                  <div className={styles.visualizerGrid}>
                    {visualizerImages.map((image) => (
                      <div key={image.name} className={styles.visualizerCard}>
                        <img
                          src={formatVisualizerAssetUrl(image.url)}
                          alt={image.name}
                          className={styles.visualizerImage}
                        />
                        <span className={styles.visualizerCaption}>{image.name}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className={styles.visualizerStatus}>
                    No visualizations found yet. Run the Allure visualizer or execute tests to generate charts under
                    the backend's allure_reports folder.
                  </p>
                )
              )}
            </div>
          )}
        </div>
      </div>

      {/* Back Button */}
      <div className={styles.backButtonContainer}>
        <button onClick={onBack} className={styles.backButton}>
          <i className="fa-solid fa-angle-left"></i>
          Back
        </button>
      </div>
    </div>
  );
};

export default Execute;

