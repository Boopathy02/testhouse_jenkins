import React, { useMemo, useState } from "react";
import { toast } from "react-toastify";
import API_BASE_URL from "../config";
import styles from "../css/EtlValidation.module.css";

const DEFAULT_CONSTRAINTS = [
  {
    id: "schema",
    title: "Schema Validation",
    description: "Verify data types, column names, and table structures.",
  },
  {
    id: "row-count",
    title: "Row Count Reconciliation",
    description: "Compare source and target row counts.",
  },
  {
    id: "domain",
    title: "Domain Validation",
    description: "Validate data against business rules and ranges.",
  },
  {
    id: "idempotency",
    title: "Idempotency Checks",
    description: "Ensure repeated loads don't create duplicate data.",
  },
  {
    id: "referential",
    title: "Referential Integrity",
    description: "Check foreign key relationships and orphaned records.",
  },
  {
    id: "duplicate",
    title: "Duplicate Checks",
    description: "Identify duplicate records based on key columns.",
  },
  {
    id: "nulls",
    title: "Null Value Checks",
    description: "Detect NULL values in NOT NULL columns.",
  },
  {
    id: "accuracy",
    title: "Data Accuracy",
    description: "Validate aggregate and value-level parity between source and target.",
  },
  {
    id: "transformation",
    title: "Transformation Checks",
    description: "Validate mapping rules, derived fields, and transformation logic.",
  },
  {
    id: "cross-table",
    title: "Cross-table Consistency",
    description: "Validate consistency across related entities and linked tables.",
  },
  {
    id: "historical",
    title: "Historical Consistency",
    description: "Detect abnormal batch-over-batch shifts and incremental anomalies.",
  },
];

const EtlValidation = ({ onNext, onBack, selectedFiles = [] }) => {
  const allIds = useMemo(() => DEFAULT_CONSTRAINTS.map((c) => c.id), []);
  const [selectedIds, setSelectedIds] = useState(() => new Set(allIds));
  const [mode, setMode] = useState("manual");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [hasGenerated, setHasGenerated] = useState(false);

  const selectedCount = selectedIds.size;

  const toggleConstraint = (id) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const handleSelectAll = () => {
    setSelectedIds(new Set(allIds));
  };

  const handleDeselectAll = () => {
    setSelectedIds(new Set());
  };

  const handleGenerate = async () => {
    if (!mode) {
      toast.warn("Select a test case generation mode.");
      return;
    }
    if (selectedIds.size === 0) {
      toast.warn("Select at least one validation constraint.");
      return;
    }
    if (!selectedFiles.length) {
      toast.warn("Please upload at least one ETL file first.");
      return;
    }

    const constraintsPayload = {
      schemaValidation: selectedIds.has("schema"),
      reconciliation: selectedIds.has("row-count"),
      domainChecks: selectedIds.has("domain"),
      idempotencyChecks: selectedIds.has("idempotency"),
      referentialIntegrity: selectedIds.has("referential"),
      duplicateChecks: selectedIds.has("duplicate"),
      nullChecks: selectedIds.has("nulls"),
      accuracyChecks: selectedIds.has("accuracy"),
      transformationChecks: selectedIds.has("transformation"),
      crossTableConsistency: selectedIds.has("cross-table"),
      historicalChecks: selectedIds.has("historical"),
    };

    const targetFile = selectedFiles[0];
    if (selectedFiles.length > 1) {
      toast.info(`Using ${targetFile.name} to generate test cases.`);
    }

    try {
      setIsSubmitting(true);
      const formData = new FormData();
      formData.append("file", targetFile);
      formData.append("mode", mode);
      formData.append("constraints", JSON.stringify(constraintsPayload));

      const response = await fetch(`${API_BASE_URL}/etl/generate-testcases`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const txt = await response.text().catch(() => null);
        throw new Error(txt || `Server returned ${response.status}`);
      }

      const result = await response.json().catch(() => null);
      if (result?.selection_message || result?.message) {
        toast.info(result.selection_message || result.message);
      }
      const projectKey = localStorage.getItem("projectId") || "default";
      if (result?.test_cases) {
        sessionStorage.setItem(
          `testify:${projectKey}:etl:test_cases`,
          JSON.stringify(result.test_cases)
        );
      }
      if (result?.summary) {
        sessionStorage.setItem(
          `testify:${projectKey}:etl:test_summary`,
          JSON.stringify(result.summary)
        );
      }
      if (result?.generated_output_path || result?.generated_pytest_path) {
        sessionStorage.setItem(
          `testify:${projectKey}:etl:pytest_path`,
          result.generated_output_path || result.generated_pytest_path
        );
      }
      toast.success("Test cases generated successfully.");
      setHasGenerated(true);
    } catch (err) {
      toast.error(`Failed to generate test cases: ${err?.message || err}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <div className={styles.validationContainer}>
      <section className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <h2 className={styles.panelTitle}>Validation Constraints</h2>
            <p className={styles.panelSubtitle}>
              Select which validation checks to run on your data during ETL processing
            </p>
          </div>
          <div className={styles.panelActions}>
            <button type="button" className={styles.linkButton} onClick={handleSelectAll}>
              Select All
            </button>
            <button type="button" className={styles.linkButton} onClick={handleDeselectAll}>
              Deselect All
            </button>
          </div>
        </div>

        <div className={styles.constraintGrid}>
          {DEFAULT_CONSTRAINTS.map((constraint) => {
            const checked = selectedIds.has(constraint.id);
            return (
              <label key={constraint.id} className={styles.constraintItem}>
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggleConstraint(constraint.id)}
                />
                <div>
                  <span className={styles.constraintTitle}>{constraint.title}</span>
                  <span className={styles.constraintDescription}>{constraint.description}</span>
                </div>
              </label>
            );
          })}
        </div>

        <div className={styles.panelFooter}>
          <span className={styles.panelFooterLabel}>Active Constraints:</span>
          <strong>{selectedCount} of {DEFAULT_CONSTRAINTS.length} selected</strong>
        </div>
      </section>

      <section className={styles.panel}>
        <div className={styles.panelHeader}>
          <div>
            <h2 className={styles.panelTitle}>Test Case Generation</h2>
            <p className={styles.panelSubtitle}>
              Choose how you want to generate validation test cases for your data
            </p>
          </div>
        </div>

        <div className={styles.modeGrid}>
          <button
            type="button"
            className={`${styles.modeCard} ${mode === "manual" ? styles.modeCardActive : ""}`}
            onClick={() => {
              setMode("manual");
              toast.info("Selected as manual");
            }}
          >
            <div className={styles.modeIcon}>
              <i className="fa-regular fa-file-lines"></i>
            </div>
            <div className={styles.modeContent}>
              <h3>Manual Test Cases</h3>
              <p>Define and configure custom validation rules manually for precise control.</p>
              <ul className={styles.modeList}>
                <li>Custom test configuration</li>
                <li>Fine-grained control</li>
                <li>Requires technical expertise</li>
              </ul>
            </div>
          </button>
          <button
            type="button"
            className={`${styles.modeCard} ${mode === "automation" ? styles.modeCardActive : ""}`}
            onClick={() => {
              setMode("automation");
              toast.info("Selected as automation");
            }}
          >
            <div className={styles.modeIcon}>
              <i className="fa-solid fa-bolt"></i>
            </div>
            <div className={styles.modeContent}>
              <h3>Automation Test Cases</h3>
              <p>Automatically generate test cases based on profiling and schema analysis.</p>
              <ul className={styles.modeList}>
                <li>AI-powered generation</li>
                <li>Quick setup & deployment</li>
                <li>Intelligent recommendations</li>
              </ul>
            </div>
          </button>
        </div>
        <p className={styles.panelSubtitle}>Selected as {mode}</p>

        <div className={styles.actionRow}>
          <button type="button" className={styles.backButton} onClick={onBack}>
            Back
          </button>
          <button
            type="button"
            className={styles.generateButton}
            onClick={handleGenerate}
            disabled={isSubmitting}
          >
            {isSubmitting ? "Generating..." : "Generate Test Cases"}
          </button>
          <button
            type="button"
            className={styles.nextButton}
            onClick={onNext}
            disabled={!hasGenerated || isSubmitting}
          >
            Next
          </button>
        </div>
      </section>
    </div>
  );
};

export default EtlValidation;
