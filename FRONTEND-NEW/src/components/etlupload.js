import React, { useEffect, useMemo, useState } from "react";
import { toast } from "react-toastify";
import API_BASE_URL from "../config";
import styles from "../css/EtlUpload.module.css";

const EtlUpload = ({
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
  const [uploadMode, setUploadMode] = useState("file");
  const [uploadResult, setUploadResult] = useState(null);
  const [dbConfig, setDbConfig] = useState({
    dbType: "PostgreSQL",
    host: "",
    port: "",
    database: "",
    username: "",
    password: "",
    schema: "",
  });

  const selectedFiles = persistedFiles;
  const setSelectedFiles = setPersistedFiles;

  const uploadFlagKey = useMemo(() => {
    const id = projectId || localStorage.getItem("projectId");
    const name = projectName || localStorage.getItem("activeProjectName") || "default";
    return `testify:${id || name}:etlUploaded`;
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
    return lower.endsWith(".json") || lower.endsWith(".csv") || lower.endsWith(".sql");
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
        toast.warn(`${file.name} is not a JSON, CSV, or SQL file.`);
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

  const handleDbFieldChange = (field, value) => {
    setDbConfig((prev) => ({
      ...prev,
      [field]: value,
    }));
  };

  const handleTestConnection = () => {
    toast.info("Test Connection is not wired yet.");
  };

  const handleConnectImport = () => {
    toast.info("Connect & Import is not wired yet.");
  };

  const handleContinue = async () => {
    setLoadingIngestion(true);
    setError("");
    setIngestionSuccess(false);
    setUploadResult(null);

    if (uploadMode !== "file") {
      toast.warn("Switch to Upload File to upload ETL files.");
      setLoadingIngestion(false);
      return;
    }

    if (selectedFiles.length === 0) {
      toast.warn("Please upload at least one JSON, CSV, or SQL file.");
      setLoadingIngestion(false);
      return;
    }

    try {
      for (const file of selectedFiles) {
        const formData = new FormData();
        formData.append("file", file);

        const response = await fetch(`${API_BASE_URL}/etl/upload-data`, {
          method: "POST",
          body: formData,
        });

        if (!response.ok) {
          const txt = await response.text().catch(() => null);
          throw new Error(txt || `Server returned ${response.status}`);
        }

        const payload = await response.json().catch(() => null);
        if (payload?.storage_error) {
          throw new Error(payload.storage_error);
        }
        if (payload) {
          setUploadResult(payload);
        }
      }

      setIngestionSuccess(true);
      sessionStorage.setItem(uploadFlagKey, "true");
      toast.success(
        `Uploaded and stored ${selectedFiles.length} file${selectedFiles.length > 1 ? "s" : ""} in database.`
      );
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
        <h2 className={styles.uploadTitle}>Import Validation Data</h2>
        <p className={styles.uploadSubtitle}>
          Upload files or connect to a database to ingest ETL validation data
        </p>

        <div className={styles.modeTabs}>
          <button
            type="button"
            className={`${styles.modeTab} ${uploadMode === "file" ? styles.modeTabActive : ""}`}
            onClick={() => setUploadMode("file")}
          >
            <i className="fa-regular fa-file-lines"></i>
            Upload File
          </button>
          <button
            type="button"
            className={`${styles.modeTab} ${uploadMode === "db" ? styles.modeTabActive : ""}`}
            onClick={() => setUploadMode("db")}
          >
            <i className="fa-solid fa-database"></i>
            Database Connection
          </button>
        </div>

        {uploadMode === "file" ? (
          <>
            <div className={styles.dropzone}>
              <input
                id="etl-file-upload"
                type="file"
                accept=".json,.csv,.sql,application/json,text/csv,text/plain"
                multiple
                style={{ display: "none" }}
                onChange={handleFileChange}
              />

              <i className={`fa-solid fa-cloud-arrow-up ${styles.uploadIcon}`}></i>

              <h3 className={styles.uploadText}>Upload ETL Files</h3>

              <p className={styles.uploadInstructions}>
                Only JSON, CSV, and SQL files are supported
              </p>

              <button
                type="button"
                onClick={() => document.getElementById("etl-file-upload").click()}
                className={styles.selectFilesButton}
              >
                <i className={`fa-solid fa-upload ${styles.selectFilesButtonIcon}`}></i>
                <span className={styles.selectFilesButtonText}>Select Files</span>
              </button>
            </div>

            {selectedFiles.length > 0 && (
              <div className={styles.availablePagesContainer}>
                <h4 className={styles.availablePagesTitle}>Selected ETL files</h4>
                <ul className={styles.pageList}>
                  {selectedFiles.map((file, idx) => (
                    <li key={`${file.name}-${idx}`} className={styles.pageListItem}>
                      <span>{file.name}</span>
                      <button
                        type="button"
                        onClick={() => handleRemove(idx)}
                        className={styles.removeButton}
                      >
                        Remove
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        ) : (
          <div className={styles.dbForm}>
            <label className={styles.formLabel}>
              Database Type
              <select
                value={dbConfig.dbType}
                onChange={(e) => handleDbFieldChange("dbType", e.target.value)}
                className={styles.formInput}
              >
                <option>PostgreSQL</option>
                <option>MySQL</option>
                <option>SQL Server</option>
                <option>Oracle</option>
                <option>Snowflake</option>
                <option>BigQuery</option>
              </select>
            </label>

            <div className={styles.formRow}>
              <label className={styles.formLabel}>
                Host
                <input
                  type="text"
                  value={dbConfig.host}
                  onChange={(e) => handleDbFieldChange("host", e.target.value)}
                  placeholder="localhost"
                  className={styles.formInput}
                />
              </label>
              <label className={styles.formLabel}>
                Port
                <input
                  type="text"
                  value={dbConfig.port}
                  onChange={(e) => handleDbFieldChange("port", e.target.value)}
                  placeholder="5432"
                  className={styles.formInput}
                />
              </label>
            </div>

            <label className={styles.formLabel}>
              Database Name
              <input
                type="text"
                value={dbConfig.database}
                onChange={(e) => handleDbFieldChange("database", e.target.value)}
                placeholder="etl_validation"
                className={styles.formInput}
              />
            </label>

            <div className={styles.formRow}>
              <label className={styles.formLabel}>
                Username
                <input
                  type="text"
                  value={dbConfig.username}
                  onChange={(e) => handleDbFieldChange("username", e.target.value)}
                  placeholder="admin"
                  className={styles.formInput}
                />
              </label>
              <label className={styles.formLabel}>
                Password
                <input
                  type="password"
                  value={dbConfig.password}
                  onChange={(e) => handleDbFieldChange("password", e.target.value)}
                  placeholder="••••••••"
                  className={styles.formInput}
                />
              </label>
            </div>

            <label className={styles.formLabel}>
              Schema/Table (Optional)
              <input
                type="text"
                value={dbConfig.schema}
                onChange={(e) => handleDbFieldChange("schema", e.target.value)}
                placeholder="validation_results"
                className={styles.formInput}
              />
            </label>

            <div className={styles.dbActions}>
              <button type="button" className={styles.secondaryButton} onClick={handleTestConnection}>
                Test Connection
              </button>
              <button type="button" className={styles.primaryButton} onClick={handleConnectImport}>
                Connect & Import
              </button>
            </div>
          </div>
        )}

        {error && <p className={styles.errorText}>{error}</p>}

        {ingestionSuccess && (
          <div className={styles.successMessage}>
            ETL files saved for this project.
          </div>
        )}

        {uploadResult && (
          <div className={styles.successMessage}>
            Tables: {(uploadResult.stored_tables || []).join(", ") || "n/a"}
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
          // disabled={!ingestionSuccess && !allowSkipUpload}
          className={styles.nextButton}
        >
          Next <i className="fa-solid fa-angle-right"></i>
        </button>
      </div>
    </div>
  );
};

export default EtlUpload;
