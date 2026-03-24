import React, { useMemo, useState } from "react";
import { toast } from "react-toastify";
import API_BASE_URL from "../config";
import styles from "../css/EtlExecute.module.css";

const EtlExecute = ({ onBack }) => {
  const [isRunning, setIsRunning] = useState(false);
  const [results, setResults] = useState(null);
  const [error, setError] = useState("");
  const [showDetails, setShowDetails] = useState(false);
  const [showAllTests, setShowAllTests] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [filterType, setFilterType] = useState("All Types");
  const [filterStatus, setFilterStatus] = useState("All Status");

  const projectKey = useMemo(
    () => localStorage.getItem("projectId") || "default",
    []
  );

  const handleExecute = async () => {
    setIsRunning(true);
    setError("");
    try {
      const pytestPath = sessionStorage.getItem(`testify:${projectKey}:etl:pytest_path`);
      const response = await fetch(`${API_BASE_URL}/etl/execute-tests`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ pytest_path: pytestPath || "" }),
      });
      if (!response.ok) {
        const txt = await response.text().catch(() => null);
        throw new Error(txt || `Server returned ${response.status}`);
      }
      const payload = await response.json();
      setResults(payload);
      toast.success("ETL tests executed.");
    } catch (err) {
      setError(err?.message || "Failed to execute ETL tests.");
      toast.error(err?.message || "Failed to execute ETL tests.");
    } finally {
      setIsRunning(false);
    }
  };

  const counts = results?.summary?.counts || {};
  const total = results?.summary?.total || 0;
  const passed = counts.passed || 0;
  const failed = counts.failed || 0;
  const skipped = counts.skipped || 0;
  const errors = counts.errors || 0;
  const status = failed > 0 || errors > 0 ? "FAIL" : total > 0 ? "PASS" : "N/A";
  const pytestPath = results?.pytest_path || "";
  const storedCasesRaw = sessionStorage.getItem(`testify:${projectKey}:etl:test_cases`);
  const storedCases = useMemo(() => {
    if (!storedCasesRaw) return [];
    try {
      const parsed = JSON.parse(storedCasesRaw);
      return Array.isArray(parsed) ? parsed : [];
    } catch (err) {
      return [];
    }
  }, [storedCasesRaw]);

  const batchMeta = useMemo(() => {
    if (!pytestPath) return null;
    const match = pytestPath.match(/(\d{8})_(\d{6})/);
    const dateRaw = match?.[1] || "";
    const loadDate = dateRaw
      ? `${dateRaw.slice(0, 4)}-${dateRaw.slice(4, 6)}-${dateRaw.slice(6, 8)}`
      : "";
    const summaryLine = results?.summary?.summary_line || "";
    const execMatch = summaryLine.match(/in\s+([0-9.]+s)/i);
    return {
      batchId: `BATCH_${dateRaw || "NA"}_001`,
      loadDate: loadDate || "n/a",
      executionTime: execMatch?.[1] || "n/a",
    };
  }, [pytestPath, results?.summary?.summary_line]);

  const detailsCards = useMemo(() => {
    return [
      { key: "schema", title: "Schema Validation" },
      { key: "nulls", title: "Null Value Checks" },
      { key: "domain", title: "Domain Checks" },
      { key: "reconciliation", title: "Reconciliation" },
      { key: "idempotency", title: "Idempotency Checks" },
    ];
  }, []);

  if (showAllTests) {
    const availableTypes = Array.from(
      new Set(storedCases.map((t) => t.validation_type).filter(Boolean))
    );
    const filteredCases = storedCases.filter((test) => {
      const matchesSearch = searchTerm
        ? `${test.test_name || ""} ${test.tables?.join(" ") || ""}`
            .toLowerCase()
            .includes(searchTerm.toLowerCase())
        : true;
      const matchesType =
        filterType === "All Types" ? true : test.validation_type === filterType;
      const matchesStatus =
        filterStatus === "All Status" ? true : test.status === filterStatus;
      return matchesSearch && matchesType && matchesStatus;
    });

    return (
      <div className={styles.detailsContainer}>
        <div className={styles.resultsHeader}>
          <button
            type="button"
            className={styles.detailsBackButton}
            onClick={() => setShowAllTests(false)}
          >
            Back
          </button>
          <h2 className={styles.resultsTitle}>Validation Test Results</h2>
        </div>

        <div className={styles.filtersPanel}>
          <div className={styles.filtersTitle}>Filters</div>
          <div className={styles.filtersRow}>
            <div className={styles.searchInput}>
              <input
                type="text"
                placeholder="Search by test name or table..."
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
              />
            </div>
            <select
              className={styles.filterSelect}
              value={filterType}
              onChange={(e) => setFilterType(e.target.value)}
            >
              <option>All Types</option>
              {availableTypes.map((type) => (
                <option key={type}>{type}</option>
              ))}
            </select>
            <select
              className={styles.filterSelect}
              value={filterStatus}
              onChange={(e) => setFilterStatus(e.target.value)}
            >
              <option>All Status</option>
              <option>PASS</option>
              <option>FAIL</option>
            </select>
          </div>
          <div className={styles.filtersMeta}>
            Showing {filteredCases.length} of {storedCases.length} test results
          </div>
        </div>

        <div className={styles.resultsTable}>
          <div className={styles.resultsRowHeader}>
            <span>Test ID</span>
            <span>Test Name</span>
            <span>Validation Type</span>
            <span>Table(s)</span>
            <span>Status</span>
            <span>Error Message</span>
            <span>Execution Time</span>
          </div>
          {filteredCases.length === 0 ? (
            <div className={styles.resultsEmpty}>No test results to display.</div>
          ) : (
            filteredCases.map((test) => (
              <div key={test.test_id} className={styles.resultsRow}>
                <span className={styles.mono}>{test.test_id}</span>
                <span className={styles.resultsName}>{test.test_name}</span>
                <span>{test.validation_type}</span>
                <span className={styles.tableTags}>
                  {(test.tables || []).map((t) => (
                    <span key={t} className={styles.tableTag}>
                      {t}
                    </span>
                  ))}
                </span>
                <span>
                  <span
                    className={
                      test.status === "PASS" ? styles.statusPillPass : styles.statusPillFail
                    }
                  >
                    {test.status}
                  </span>
                </span>
                <span className={styles.resultsError}>
                  {test.error_message || "—"}
                </span>
                <span>{test.execution_time || "n/a"}</span>
              </div>
            ))
          )}
        </div>
      </div>
    );
  }

  if (showDetails && batchMeta) {
    return (
      <div className={styles.detailsContainer}>
        <div className={styles.detailsHeader}>
          <div className={styles.detailsLeft}>
            <button
              type="button"
              className={styles.detailsBackButton}
              onClick={() => setShowDetails(false)}
            >
              Back
            </button>
            <div className={styles.detailsTitleGroup}>
              <h2 className={styles.detailsBatchTitle}>{batchMeta.batchId}</h2>
              <span
                className={
                  status === "PASS" ? styles.statusPillPass : styles.statusPillFail
                }
              >
                {status}
              </span>
            </div>
            <div className={styles.detailsMeta}>
              <span>{batchMeta.loadDate}</span>
              <span className={styles.metaDivider}>•</span>
              <span>{batchMeta.executionTime}</span>
            </div>
          </div>
          <div className={styles.detailsRight}>
            <button
              type="button"
              className={styles.secondaryAction}
              onClick={() => {
                setShowAllTests(true);
                setShowDetails(false);
              }}
            >
              View All Tests
            </button>
            <button type="button" className={styles.secondaryAction}>
              Reconciliation
            </button>
          </div>
        </div>

        <div className={styles.detailsStats}>
          <div className={styles.statCard}>
            <div className={styles.statValue}>{total}</div>
            <div className={styles.statLabel}>Total Tests</div>
          </div>
          <div className={`${styles.statCard} ${styles.statCardPass}`}>
            <div className={styles.statValue}>{passed}</div>
            <div className={styles.statLabel}>Passed</div>
          </div>
          <div className={`${styles.statCard} ${styles.statCardFail}`}>
            <div className={styles.statValue}>{failed}</div>
            <div className={styles.statLabel}>Failed</div>
          </div>
          <div className={`${styles.statCard} ${styles.statCardWarn}`}>
            <div className={styles.statValue}>{skipped}</div>
            <div className={styles.statLabel}>Warnings</div>
          </div>
        </div>

        <div className={styles.validationSection}>
          <h3 className={styles.sectionTitle}>Validation Summary</h3>
          <div className={styles.validationGrid}>
            {detailsCards.map((card) => (
              <div key={card.key} className={styles.validationCard}>
                <div className={styles.validationTop}>
                  <h4>{card.title}</h4>
                  <span className={styles.validationStatusIcon}></span>
                </div>
                <div className={styles.validationRow}>
                  <span>Status:</span>
                  <span
                    className={
                      status === "PASS" ? styles.statusPillPass : styles.statusPillFail
                    }
                  >
                    {status}
                  </span>
                </div>
                <div className={styles.validationRow}>
                  <span>Tests:</span>
                  <span className={styles.validationTests}>
                    {Math.max(1, Math.round(total / detailsCards.length))} /{" "}
                    {Math.max(1, Math.round(total / detailsCards.length))}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className={styles.quickActions}>
          <div className={styles.quickActionsTitle}>Quick Actions</div>
          <div className={styles.quickActionsRow}>
            <button type="button" className={styles.quickActionButton}>
              View Data Quality Issues
            </button>
            <button type="button" className={styles.quickActionButton}>
              View Reconciliation Details
            </button>
            <button
              type="button"
              className={styles.quickActionButton}
              onClick={() => {
                setShowAllTests(true);
                setShowDetails(false);
              }}
            >
              View All Test Results
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className={styles.container}>
      <div className={styles.header}>
        <div>
          <h2 className={styles.title}>ETL Validation Dashboard</h2>
          <p className={styles.subtitle}>
            Run the latest ETL pytest file and review results.
          </p>
        </div>
        <button
          type="button"
          className={styles.executeButton}
          onClick={handleExecute}
          disabled={isRunning}
        >
          {isRunning ? "Executing..." : "Execute"}
        </button>
      </div>

      <div className={styles.cards}>
        <div className={styles.card}>
          <div className={styles.cardLabel}>Overall Status</div>
          <div className={status === "PASS" ? styles.statusPass : styles.statusFail}>
            {status}
          </div>
        </div>
        <div className={styles.card}>
          <div className={styles.cardLabel}>Total Tests Run</div>
          <div className={styles.cardValue}>{total}</div>
        </div>
        <div className={styles.card}>
          <div className={styles.cardLabel}>Passed Tests</div>
          <div className={styles.cardValue}>{passed}</div>
        </div>
        <div className={styles.card}>
          <div className={styles.cardLabel}>Failed Tests</div>
          <div className={styles.cardValue}>{failed}</div>
        </div>
        <div className={styles.card}>
          <div className={styles.cardLabel}>Warnings / Skipped</div>
          <div className={styles.cardValue}>{skipped}</div>
        </div>
      </div>

      {results?.summary?.summary_line && (
        <div className={styles.summaryLine}>
          {results.summary.summary_line}
        </div>
      )}

      <div className={styles.recentPanel}>
        <div className={styles.recentHeader}>Recent Batch Executions</div>
        <div className={styles.recentTable}>
          <div className={styles.recentRowHeader}>
            <span>Batch ID</span>
            <span>Load Date</span>
            <span>Status</span>
            <span>Tests</span>
            <span>Execution Time</span>
            <span className={styles.actionHeader}>Action</span>
          </div>
          {batchMeta ? (
            <div className={styles.recentRow}>
              <span className={styles.batchId}>{batchMeta.batchId}</span>
              <span>{batchMeta.loadDate}</span>
              <span>
                <span
                  className={
                    status === "PASS" ? styles.statusPillPass : styles.statusPillFail
                  }
                >
                  {status}
                </span>
              </span>
              <span className={styles.testsCell}>
                <span className={styles.testsPass}>{passed}</span>
                <span className={styles.testsDivider}> / </span>
                <span className={styles.testsFail}>{failed}</span>
                <span className={styles.testsDivider}> / </span>
                <span className={styles.testsTotal}>{total}</span>
              </span>
              <span>{batchMeta.executionTime}</span>
              <span className={styles.actionCell}>
                <button
                  type="button"
                  className={styles.viewButton}
                  onClick={() => setShowDetails((prev) => !prev)}
                >
                  View Details
                </button>
              </span>
            </div>
          ) : (
            <div className={styles.recentEmpty}>No batch executions yet.</div>
          )}
        </div>
        {showDetails && results && (
          <div className={styles.detailsPanel}>
            <div className={styles.detailsTitle}>Execution Details</div>
            <div className={styles.detailsGrid}>
              <div>
                <div className={styles.detailLabel}>Pytest Path</div>
                <div className={styles.detailValue}>{pytestPath || "n/a"}</div>
              </div>
              <div>
                <div className={styles.detailLabel}>Summary</div>
                <div className={styles.detailValue}>
                  {results?.summary?.summary_line || "n/a"}
                </div>
              </div>
            </div>
            {results?.stderr ? (
              <pre className={styles.detailLog}>{results.stderr}</pre>
            ) : null}
          </div>
        )}
      </div>

      {error && <div className={styles.error}>{error}</div>}

      <div className={styles.backRow}>
        <button type="button" className={styles.backButton} onClick={onBack}>
          Back
        </button>
      </div>
    </div>
  );
};

export default EtlExecute;
