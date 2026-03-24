import React, { useCallback, useEffect, useState } from "react";
import API_BASE_URL from "../config";
import { toast } from "react-toastify";
import styles from "../css/PageMethods.module.css";

const ApiMethods = ({ onBack, onNext, pageNames, setPageNames, projectName, projectId }) => {
  const [loadingMethods, setLoadingMethods] = useState(false);
  const [activeProjectId, setActiveProjectId] = useState(() => projectId || localStorage.getItem("projectId"));
  const [hasGeneratedMethods, setHasGeneratedMethods] = useState(false);
  const [allowSkipMethods, setAllowSkipMethods] = useState(false);

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

  const storageKey = useCallback(() => {
    const id =
      activeProjectId ||
      projectId ||
      localStorage.getItem("projectId") ||
      localStorage.getItem("activeProjectName") ||
      "default";
    return `testify:${id}:apiMethods`;
  }, [activeProjectId, projectId]);

  const existingProjectKey = useCallback(() => {
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
      const saved = sessionStorage.getItem(storageKey());
      if (saved) {
        const parsed = JSON.parse(saved);
        if (Array.isArray(parsed.pageNames)) {
          setPageNames(parsed.pageNames);
          if (parsed.pageNames.length > 0) {
            setHasGeneratedMethods(true);
          }
        }
      }
      const flag = sessionStorage.getItem(`${storageKey()}:generated`);
      if (flag === "true") {
        setHasGeneratedMethods(true);
      }
      const existingFlag = sessionStorage.getItem(existingProjectKey());
      if (existingFlag === "true") {
        setAllowSkipMethods(true);
      }
    } catch (err) {
      console.warn("Failed to restore API methods state:", err);
    }
  }, [storageKey, existingProjectKey, setPageNames]);

  useEffect(() => {
    try {
      sessionStorage.setItem(storageKey(), JSON.stringify({ pageNames }));
      if (Array.isArray(pageNames) && pageNames.length > 0) {
        sessionStorage.setItem(`${storageKey()}:generated`, "true");
        setHasGeneratedMethods(true);
      }
    } catch (err) {
      console.warn("Failed to persist API methods state:", err);
    }
  }, [pageNames, storageKey]);

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
      console.error("Failed to activate project before loading API methods:", err);
      toast.error(err.message || "Failed to activate project.");
      return null;
    }
  }, [activeProjectId, projectName]);

  const handleGenerateMethods = async () => {
    setLoadingMethods(true);
    try {
      const activeId = await ensureActiveProject();
      if (!activeId) {
        throw new Error("No active project. Please start a project first.");
      }
      const token = localStorage.getItem("token");
      const response = await fetch(`${API_BASE_URL}/api-tests/generate-page-file`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({}),
      });
      if (!response.ok) {
        const txt = await response.text().catch(() => null);
        throw new Error(txt || `Failed to generate API methods (${response.status})`);
      }
      const data = await response.json();
      const services = Array.isArray(data?.services) ? data.services : [];
      const serviceNames = services.map((service) => service?.service).filter(Boolean);
      const names = Array.from(new Set(serviceNames));
      setPageNames(names);
      setHasGeneratedMethods(true);
      sessionStorage.setItem(`${storageKey()}:generated`, "true");
      toast.success("Loaded API methods");
    } catch (error) {
      console.error("Error fetching API methods:", error);
      setPageNames([]);
      toast.error(error?.message || "Error loading API methods");
    } finally {
      setLoadingMethods(false);
    }
  };

  return (
    <div className={styles.methodsContainer}>
      <div className={styles.methodsPanel}>
        <div>
          <h2 className={styles.methodsTitle}>Generate API Methods</h2>
          <p className={styles.methodsSubtitle}>
            Use API specs to prepare reusable service methods.
          </p>
        </div>

        <div className={styles.methodsActions}>
          <button
            onClick={handleGenerateMethods}
            disabled={loadingMethods}
            className={styles.primaryButton}
          >
            {loadingMethods ? (
              <>
                <span className="globalSpinner" aria-hidden="true"></span>
                Generating...
              </>
            ) : (
              "Generate API Methods"
            )}
          </button>
        </div>

        {pageNames.length > 0 ? (
          <div className={styles.availablePagesContainer}>
            <h5 className={styles.availablePagesTitle}>Generated Services</h5>
            <ul className={styles.pageList}>
              {pageNames.map((name, idx) => (
                <li key={idx} className={styles.pageListItem}>
                  {name}
                </li>
              ))}
            </ul>
          </div>
        ) : (
          <div className={styles.placeholder}>
            No API methods generated yet.
          </div>
        )}
      </div>

      <div className={styles.navigationRow}>
        <button onClick={onBack} className={`${styles.secondaryButton} ${styles.navButton}`}>
          <i className="fa-solid fa-angle-left"></i>
          Back
        </button>
        <button
          onClick={onNext}
          disabled={
            (!Array.isArray(pageNames) || pageNames.length === 0) &&
            !hasGeneratedMethods &&
            !allowSkipMethods
          }
          className={`${styles.primaryButton} ${styles.navButton}`}
        >
          Next
          <i className="fa-solid fa-angle-right"></i>
        </button>
      </div>
    </div>
  );
};

export default ApiMethods;
