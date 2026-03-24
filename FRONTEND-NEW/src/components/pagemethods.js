import React, { useCallback, useEffect, useState } from "react";
import API_BASE_URL from "../config";
import { toast } from "react-toastify";
import styles from "../css/PageMethods.module.css";

const PageMethods = ({ onBack, onNext, pageNames, setPageNames, projectName, projectId }) => {
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
    return `testify:${id}:pageMethods`;
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
      console.warn("Failed to restore page methods state:", err);
    }
  }, [storageKey, existingProjectKey, setPageNames]);

  useEffect(() => {
    try {
      sessionStorage.setItem(
        storageKey(),
        JSON.stringify({ pageNames })
      );
      if (Array.isArray(pageNames) && pageNames.length > 0) {
        sessionStorage.setItem(`${storageKey()}:generated`, "true");
        setHasGeneratedMethods(true);
      }
    } catch (err) {
      console.warn("Failed to persist page methods state:", err);
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
      console.error("Failed to activate project before generating methods:", err);
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
      const response = await fetch(`${API_BASE_URL}/${activeId}/rag/generate-page-methods`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({}),
      });

      const data = await response.json();
      const names = Object.keys(data || {});
      setPageNames(names);
      setHasGeneratedMethods(true);
      sessionStorage.setItem(`${storageKey()}:generated`, "true");
      toast.success("Successfully generated methods");
    } catch (error) {
      console.error("Error fetching page methods:", error);
      setPageNames([]);
      toast.error("Error generating methods");
    } finally {
      setLoadingMethods(false);
    }
  };

  return (
    <div className={styles.methodsContainer}>
      <div className={styles.methodsPanel}>
        <div>
          <h2 className={styles.methodsTitle}>Generate Page Methods</h2>
          <p className={styles.methodsSubtitle}>
            Use OCR output to generate page objects and reusable automation methods.
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
              "Generate Page Methods"
            )}
          </button>
        </div>

        {pageNames.length > 0 ? (
          <div className={styles.availablePagesContainer}>
            <h5 className={styles.availablePagesTitle}>Generated Pages</h5>
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
            No page methods generated yet.
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

export default PageMethods;
