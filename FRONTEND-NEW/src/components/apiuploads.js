import React, { useEffect, useMemo, useState } from "react";
import { toast } from "react-toastify";
import API_BASE_URL from "../config";
import styles from "../css/ImageUpload.module.css";

const ApiUpload = ({
  handleNext,
  persistedFiles = [],
  setPersistedFiles,
  projectName,
  projectId,
}) => {
  const [loadingIngestion, setLoadingIngestion] = useState(false);
  const [ingestionSuccess, setIngestionSuccess] = useState(false);
  const [allowSkipUpload, setAllowSkipUpload] = useState(false);
  const [error, setError] = useState("");

  const selectedFiles = persistedFiles;
  const setSelectedFiles = setPersistedFiles;

  const uploadFlagKey = useMemo(() => {
    const id = projectId || localStorage.getItem("projectId");
    const name = projectName || localStorage.getItem("activeProjectName") || "default";
    return `testify:${id || name}:apiUploaded`;
  }, [projectId, projectName]);

  const existingProjectKey = useMemo(() => {
    const id = projectId || localStorage.getItem("projectId");
    const name = projectName || localStorage.getItem("activeProjectName") || "default";
    return `testify:${id || name}:existingProject`;
  }, [projectId, projectName]);

  useEffect(() => {
    const flag = sessionStorage.getItem(uploadFlagKey);
    if (flag === "true") {
      setIngestionSuccess(true);
    }
    const existingFlag = sessionStorage.getItem(existingProjectKey);
    if (existingFlag === "true") {
      setAllowSkipUpload(true);
    }
  }, [existingProjectKey, uploadFlagKey]);

  useEffect(() => {
    if (projectId) {
      localStorage.setItem("projectId", String(projectId));
    }
    if (projectName) {
      localStorage.setItem("activeProjectName", projectName);
    }
  }, [projectId, projectName]);

  const isAllowedFile = (file) => {
    const lower = (file?.name || "").toLowerCase();
    return lower.endsWith(".json") || lower.endsWith(".xml");
  };

  const handleFileChange = (e) => {
    const inputFiles = Array.from(e.target.files || []);
    if (!inputFiles.length) {
      return;
    }

    const validFiles = [];
    inputFiles.forEach((file) => {
      if (isAllowedFile(file)) {
        validFiles.push(file);
      } else {
        toast.warn(`${file.name} is not a JSON or XML file.`);
      }
    });

    if (!validFiles.length) {
      return;
    }

    setSelectedFiles((prev) => [...prev, ...validFiles]);
  };

  const handleRemove = (index) => {
    setSelectedFiles((prev) => prev.filter((_, idx) => idx !== index));
  };

  const handleContinue = async () => {
    setLoadingIngestion(true);
    setError("");
    setIngestionSuccess(false);

    if (selectedFiles.length === 0) {
      toast.warn("Please upload at least one JSON or XML file.");
      setLoadingIngestion(false);
      return;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      toast.error("Missing auth token. Please log in again.");
      setLoadingIngestion(false);
      return;
    }

    let successCount = 0;
    try {
      for (const file of selectedFiles) {
        const formData = new FormData();
        formData.append("file", file);
        const response = await fetch(`${API_BASE_URL}/api-specs/import-file`, {
          method: "POST",
          headers: {
            Authorization: `Bearer ${token}`,
          },
          body: formData,
        });

        if (!response.ok) {
          const errorText = await response.text().catch(() => null);
          throw new Error(errorText || `Upload failed with status ${response.status}`);
        }
        successCount += 1;
      }

      if (successCount > 0) {
        setIngestionSuccess(true);
        sessionStorage.setItem(uploadFlagKey, "true");
        toast.success(`Uploaded ${successCount} API file${successCount > 1 ? "s" : ""}.`);
      }
    } catch (err) {
      setError(err?.message || "Please try again.");
      toast.error(`Error uploading files: ${err?.message || "Please try again."}`);
    } finally {
      setLoadingIngestion(false);
    }
  };

  return (
    <div className={styles.imageUploadContainer}>
      <div className={styles.uploadBox}>
        <h2 className={styles.uploadTitle}>Upload API Files</h2>
        <p className={styles.uploadSubtitle}>
          Upload JSON or XML files for API test generation
        </p>

        <div className={styles.dropzone}>
          <input
            id="api-file-upload"
            type="file"
            accept=".json,.xml,application/json,application/xml,text/xml"
            multiple
            style={{ display: "none" }}
            onChange={handleFileChange}
          />

          <i className={`fa-solid fa-cloud-arrow-up ${styles.uploadIcon}`}></i>

          <h3 className={styles.uploadText}>Upload API Files</h3>

          <p className={styles.uploadInstructions}>
            Only JSON and XML files are supported
          </p>

          <button
            type="button"
            onClick={() => document.getElementById("api-file-upload").click()}
            className={styles.selectFilesButton}
          >
            <i className={`fa-solid fa-upload ${styles.selectFilesButtonIcon}`}></i>
            <span className={styles.selectFilesButtonText}>Select Files</span>
          </button>
        </div>

        {selectedFiles.length > 0 && (
          <div className={styles.availablePagesContainer}>
            <h4 className={styles.availablePagesTitle}>Selected API files</h4>
            <ul className={styles.pageList}>
              {selectedFiles.map((file, idx) => (
                <li key={`${file.name}-${idx}`} className={styles.pageListItem}>
                  <span>{file.name}</span>
                  <button
                    type="button"
                    onClick={() => handleRemove(idx)}
                    className={styles.selectFilesButton}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {error && <p className={styles.errorText}>{error}</p>}

        {ingestionSuccess && (
          <div className={styles.successMessage}>
            API files saved for this project.
          </div>
        )}

        <div className={styles.actionButtons}>
          <button
            onClick={handleContinue}
            disabled={loadingIngestion}
            className={styles.uploadImagesButton}
          >
            {loadingIngestion ? <div className={styles.spinner}></div> : "Upload Files"}
          </button>
        </div>
      </div>

      <div className={styles.nextButtonContainer}>
        <button
          onClick={handleNext}
          disabled={!ingestionSuccess && !allowSkipUpload}
          className={styles.nextButton}
        >
          Next <i className="fa-solid fa-angle-right"></i>
        </button>
      </div>
    </div>
  );
};

export default ApiUpload;
