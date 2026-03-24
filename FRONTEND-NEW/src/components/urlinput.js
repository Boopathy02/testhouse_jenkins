import React, { useState, useEffect, useCallback, useRef } from "react";
import axios from "axios";
import API_BASE_URL from "../config";
import { toast } from "react-toastify";
import styles from "../css/URLInput.module.css";

const URLInput = ({ onBack, onNext, apiMode = "ocr", projectName,
  projectId, }) => {
  const [url, setUrl] = useState("");
  const [fullTestData, setFullTestData] = useState(null);
  const [hasEnriched, setHasEnriched] = useState(false);
  const [allowSkipEnrich, setAllowSkipEnrich] = useState(false);
  const [loadingEnrich, setLoadingEnrich] = useState(false);
  const [loadingManualEnrich, setLoadingManualEnrich] = useState(false);
  const [error, setError] = useState("");
  const [activeProjectId, setActiveProjectId] = useState(() => projectId || localStorage.getItem("projectId"));
  const manualPollRef = useRef(null);

  const validateUrl = () => {
    if (!url || url.trim() === "") {
      setError("Please enter a valid URL");
      return false;
    }

    try {
      new URL(url);
    } catch (_) {
      setError("Please enter a valid URL format (e.g., https://example.com)");
      return false;
    }

    return true;
  };

  const formatError = (value) => {
    if (!value) {
      return "";
    }
    if (typeof value === "string") {
      return value;
    }
    if (Array.isArray(value)) {
      return value.map((item) => item?.msg || JSON.stringify(item)).join(", ");
    }
    if (typeof value === "object") {
      return value.msg || value.detail || JSON.stringify(value);
    }
    return String(value);
  };

  const stopManualPoll = () => {
    if (manualPollRef.current) {
      clearInterval(manualPollRef.current);
      manualPollRef.current = null;
    }
  };

  const startManualPoll = (token) => {
    stopManualPoll();
    manualPollRef.current = setInterval(async () => {
      try {
        const res = await axios.get(`${API_BASE_URL}/manual/browser-status`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.data?.closed) {
          setAllowSkipEnrich(true);
          stopManualPoll();
        }
      } catch (_) {
        // ignore polling errors
      }
    }, 1200);
  };

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

  const storageKeyPrefix = useCallback(() => {
    const id = projectId || localStorage.getItem("projectId") || "default";
    return `testify:${id}:url`;
  }, [projectId]);

  const getStorageKey = useCallback(
    (suffix) => `${storageKeyPrefix()}:${suffix}`,
    [storageKeyPrefix]
  );

  const enrichFlagKey = useCallback(
    () => `${storageKeyPrefix()}:enriched`,
    [storageKeyPrefix]
  );
  const existingProjectKey = useCallback(
    () => `${storageKeyPrefix()}:existingProject`,
    [storageKeyPrefix]
  );
  const globalExistingProjectKey = useCallback(() => {
    const id =
      activeProjectId ||
      projectId ||
      localStorage.getItem("projectId") ||
      localStorage.getItem("activeProjectName") ||
      "default";
    return `testify:${id}:existingProject`;
  }, [activeProjectId, projectId]);

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(getStorageKey("state"));
      if (saved) {
        const parsed = JSON.parse(saved);
        if (typeof parsed.url === "string") {
          setUrl(parsed.url);
        }
        if (parsed.fullTestData) {
          setFullTestData(parsed.fullTestData);
          setHasEnriched(true);
        }
      }
      const flag = sessionStorage.getItem(enrichFlagKey());
      if (flag === "true") {
        setHasEnriched(true);
      }
      const existingFlag =
        sessionStorage.getItem(existingProjectKey()) ||
        sessionStorage.getItem(globalExistingProjectKey());
      if (existingFlag === "true") {
        setAllowSkipEnrich(true);
      }
    } catch (err) {
      console.warn("Failed to restore URL state:", err);
    }
  }, [getStorageKey, enrichFlagKey, existingProjectKey, globalExistingProjectKey]);

  useEffect(() => {
    try {
      const payload = {
        url,
        fullTestData,
      };
      sessionStorage.setItem(getStorageKey("state"), JSON.stringify(payload));
      if (fullTestData) {
        sessionStorage.setItem(enrichFlagKey(), "true");
      }
    } catch (err) {
      console.warn("Failed to persist URL state:", err);
    }
  }, [url, fullTestData, getStorageKey, enrichFlagKey]);

  useEffect(() => () => stopManualPoll(), []);


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

  const enrichLocaters = async () => {
    if (!validateUrl()) {
      return;
    }

    setLoadingEnrich(true);
    setError("");
    setFullTestData(null);

    try {
      const activeId = await ensureActiveProject();
      if (!activeId) {
        throw new Error("No active project. Please start a project first.");
      }
      const token = localStorage.getItem("token");
      if (apiMode === "url") {
        const numericProjectId = Number(activeId);
        if (!Number.isInteger(numericProjectId)) {
          throw new Error("Project id is missing. Please activate a project first.");
        }
        const response = await axios.post(
          `${API_BASE_URL}/url/launch-browser`,
          { url: url.trim(), auto_enrich: true },
          {
            params: { project_id: numericProjectId },
            headers: {
              Authorization: `Bearer ${token}`,
            },
          }
        );
        const data = response.data;
        setFullTestData(data);
        setHasEnriched(true);
        sessionStorage.setItem(enrichFlagKey(), "true");
        toast.success("Locators enriched successfully");
        return;
      }
      const response = await axios.post(
        `${API_BASE_URL}/${activeId}/launch-browser`,
        { url: url.trim() },
        {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        }
      );
    const data = response.data;
    setFullTestData(data);
    setHasEnriched(true);
    sessionStorage.setItem(enrichFlagKey(), "true");
    toast.success("Locators enriched successfully");
  } catch (err) {
    const detail = err.response?.data?.detail || err.response?.data?.message || err.message;
    setError(formatError(detail) || "Error enriching locators");
  } finally {
    setLoadingEnrich(false);
  }
};

const manualEnrich = async () => {
  if (!validateUrl()) {
    return;
  }

  setLoadingManualEnrich(true);
  setError("");
  setFullTestData(null);

  try {
    const token = localStorage.getItem("token");
    if (!token) {
      toast.error("Please log in to continue.");
      return;
    }
    const response = await axios.post(
      `${API_BASE_URL}/manual/launch-browser`,
      { url: url.trim() },
      {
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
      }
    );

    toast.success(response.data?.message || "Manual enrich started");
    startManualPoll(token);
  } catch (err) {
    const detail = err.response?.data?.detail || err.response?.data?.message || err.message;
    setError(formatError(detail) || "Error starting manual enrich");
  } finally {
    setLoadingManualEnrich(false);
  }
};

return (
  <div className={styles.urlInputContainer}>
    <div className={styles.contentBox}>
      <h3 className={styles.title}>Enter URL</h3>
      <input
        type="text"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        placeholder="Paste your app URL here..."
        className={styles.urlInput}
      />
      {error && <p className={styles.errorText}>{error}</p>}

      <div className={styles.enrichButtonContainer}>
        <button
          onClick={enrichLocaters}
          className={styles.enrichButton}
        >
          {loadingEnrich ? (
            <>
              <span className="globalSpinner" aria-hidden="true"></span>
              Enriching...
            </>
          ) : (
            apiMode === "url" ? "Auto Enrich Locaters" : "Enrich Locaters"
          )}
        </button>
        {apiMode === "url" && (
          <button
            onClick={manualEnrich}
            className={styles.refreshButton}
          >
            {loadingManualEnrich ? (
              <>
                <span className="globalSpinner" aria-hidden="true"></span>
                Launching...
              </>
            ) : (
              "Manual Enrich"
            )}
          </button>
        )}
      </div>

      {fullTestData && (
        <div className={styles.testCaseOutput}>
          <h3 className={styles.testCaseOutputTitle}>
            Test Case JSON Output :
          </h3>
          <pre className={styles.jsonPre}>
            {JSON.stringify(fullTestData, null, 2)}
          </pre>
        </div>
      )}
    </div>

    <div className={styles.navigationButtons}>
      <button
        onClick={onBack}
        className={styles.navButton}
      >
        <i className="fa-solid fa-angle-left"></i>
        Previous
      </button>

      <button
        onClick={onNext}
        disabled={!fullTestData && !hasEnriched && !allowSkipEnrich}
        className={`${styles.navButton} ${styles.next}`}
      >
        Next <i className="fa-solid fa-angle-right"></i>
      </button>
    </div>
  </div>
);
};

export default URLInput;

