
import React from "react";
import styles from "../css/Dashboard.module.css";

const Dashboard = ({
  projects = [],
  onOpen,
  onDelete,
  onToggle,
  onConfigure,
  onDownload,
  onPushToGit,
  onViewReport,
  onOpenProjects,
  onStartOCRProject,
  onStartUrlExecution,
  onStartApiProject,
  onStartEtlProject,
  expandedProjectKey,
  loadingProjectKey,
  getProjectKey,
  hideRecentProjects = false,
}) => {
  const totalProjects = Array.isArray(projects) ? projects.length : 0;

  const resolveProjectKey = (project, idx) => {
    if (typeof getProjectKey === "function") {
      return getProjectKey(project, idx);
    }
    return project?.id ?? `${project?.project_name || "project"}-${idx}`;
  };

  return (
    <div className={styles.dashboardWrapper}>
      <section className={styles.summaryPanel}>
        <div className={styles.summaryHeader}>
          <div>
            <p className={styles.summaryOverline}>Portfolio</p>
            <h2 className={styles.summaryTitle}>Execution Health</h2>
          </div>
          <div className={styles.summaryMeta}>Live workspace signal</div>
        </div>
        <div className={styles.summaryGrid}>
          <button
            type="button"
            className={`${styles.summaryItem} ${styles.summaryItemButton}`}
            onClick={() => onOpenProjects && onOpenProjects()}
          >
            <span>Projects</span>
            <strong>{totalProjects}</strong>
            <i className="fa-regular fa-folder"></i>
          </button>
          <div className={styles.summaryItem}>
            <span>Test Suites</span>
            <strong>1</strong>
            <i className="fa-regular fa-file"></i>
          </div>
          <div className={styles.summaryItem}>
            <span>Active Runs</span>
            <strong>2</strong>
            <i className="fa-solid fa-play"></i>
          </div>
          <div className={styles.summaryItem}>
            <span>Frameworks</span>
            <strong>4</strong>
            <i className="fa-solid fa-code"></i>
          </div>
        </div>
      </section>

      <section className={styles.workflowPanel}>
        <div className={styles.workflowHeader}>
          <div>
            <p className={styles.summaryOverline}>Workflow</p>
            <h3>
              Ingest {"->"} Enrich {"->"} Generate {"->"} Execute
            </h3>
          </div>
          <span className={styles.workflowHint}>Launch a new pipeline</span>
        </div>
        <div className={styles.workflowList}>
          <div className={styles.workflowStep}>
            <div>
              <h4>OCR + Execution</h4>
              <p>Capture UI designs, enrich with AI, generate and execute tests.</p>
            </div>
            <button
              className={styles.workflowStartButton}
              onClick={() => onStartOCRProject && onStartOCRProject()}
            >
              Start Project with OCR 
            </button>
          </div>
          <div className={styles.workflowStep}>
            <div>
              <h4>URL + Execution</h4>
              <p>Ingest live URLs, map DOM, and generate automation scripts.</p>
            </div>
            <button
              className={styles.workflowStartButton}
              onClick={() => onStartUrlExecution && onStartUrlExecution()}
            >
              Start Project with URL
            </button>
          </div>
          <div className={styles.workflowStep}>
            <div>
              <h4>ETL Testing</h4>
              <p>Validate data pipelines, transformations, and warehouse loads.</p>
            </div>
            <button
              className={styles.workflowStartButton}
              onClick={() => onStartEtlProject && onStartEtlProject()}
            >
              Start Project with ETL
            </button>
          </div>
          <div className={styles.workflowStep}>
            <div>
              <h4>Mobile Testing</h4>
              <p>Prepare device-driven flows and regression packs.</p>
            </div>
            <button className={styles.workflowStartButton} disabled>
              Coming Soon
            </button>
          </div>
          <div className={styles.workflowStep}>
            <div>
              <h4>API Testing</h4>
              <p>Validate endpoints, payloads, and response contracts.</p>
            </div>
            <button
              className={styles.workflowStartButton}
              onClick={() => onStartApiProject && onStartApiProject()}
            >
              Start Project with API
            </button>
          </div>
        </div>
      </section>

      {hideRecentProjects ? null : (
        <section id="recent-projects" className={styles.projectPanel}>
          <div className={styles.recentProjectsHeader}>
            <h1 className={styles.recentProjectsTitle}>Recent Projects</h1>
            <button className={styles.viewAllButton}>
              View All Projects <i className="fa-solid fa-circle-chevron-down"></i>
            </button>
          </div>

          <div className={styles.projectTable}>
            <div className={styles.projectTableHeader}>
              <span>Project</span>
              <span>Framework</span>
              <span>Created</span>
              <span>Actions</span>
            </div>
            {totalProjects === 0 ? (
              <div className={styles.projectEmpty}>No projects yet. Create one to get started.</div>
            ) : (
              projects.map((p, idx) => {
                const projectKey = resolveProjectKey(p, idx);
                const isActive = expandedProjectKey === projectKey;
                const isLoading = loadingProjectKey === projectKey;

                return (
                  <div
                    key={projectKey}
                    className={`${styles.projectRow} ${isActive ? styles.projectRowActive : ""}`}
                  >
                    <div className={styles.projectCell}>
                      <div className={styles.projectName}>{p.project_name}</div>
                      <div className={styles.projectStatus}>Saved</div>
                    </div>
                    <div className={styles.projectCell}>
                      {(p.framework || "").trim()} {p.language ? ` / ${p.language}` : ""}
                    </div>
                    <div className={styles.projectCell}>
                      {p.created_at ? new Date(p.created_at).toLocaleString() : "-"}
                    </div>
                    <div className={styles.projectCell}>
                      <div className={styles.projectActions}>
                        <button
                          className={styles.actionButton}
                          onClick={() =>
                            onConfigure
                              ? onConfigure(p, projectKey)
                              : onToggle && onToggle(p, projectKey)
                          }
                        >
                          Configure
                        </button>
                        <button
                          className={styles.actionButton}
                          onClick={() => onDownload && onDownload(p)}
                        >
                          Download
                        </button>
                        <button
                          className={styles.actionButton}
                          onClick={() => onPushToGit && onPushToGit(p)}
                        >
                          Push to Git
                        </button>
                        <button
                          className={styles.actionButton}
                          onClick={() => onViewReport && onViewReport(p)}
                        >
                          Reports
                        </button>
                        <button
                          className={styles.executeButton}
                          onClick={() => onOpen && onOpen(p)}
                        >
                          Open
                        </button>
                        <button
                          type="button"
                          className={styles.deleteButton}
                          onClick={() => onDelete && onDelete(p)}
                          aria-label={`Delete ${p.project_name}`}
                        >
                          <i className="fa-solid fa-trash" aria-hidden="true"></i>
                        </button>
                      </div>
                      {isActive && isLoading && (
                        <p className={styles.projectInlineLoading}>
                          <span className="globalSpinner" aria-hidden="true"></span>
                          Loading...
                        </p>
                      )}
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </section>
      )}
    </div>
  );
};

export default Dashboard;
