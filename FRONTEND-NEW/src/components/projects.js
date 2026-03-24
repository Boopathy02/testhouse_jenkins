import React, { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "react-toastify";
import AppShell from "./AppShell";
import styles from "../css/Projects.module.css";

const Projects = () => {
  const navigate = useNavigate();
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [deletingId, setDeletingId] = useState(null);

  const apiBase = process.env.REACT_APP_API_URL || "http://127.0.0.1:8001";

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
        setError(err?.message || "Failed to load projects.");
        setProjects([]);
      } finally {
        setLoading(false);
      }
    };

    loadProjects();
  }, []);

  const sortedProjects = useMemo(() => {
    return [...projects].sort((a, b) => {
      const aDate = a?.created_at ? new Date(a.created_at).getTime() : 0;
      const bDate = b?.created_at ? new Date(b.created_at).getTime() : 0;
      return bDate - aDate;
    });
  }, [projects]);

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

  const openEditor = (project) => {
    if (!project) {
      return;
    }
    const params = new URLSearchParams();
    if (project.id) {
      params.set("projectId", String(project.id));
    }
    if (project.project_name) {
      params.set("projectName", project.project_name);
    }
    const query = params.toString();
    const url = query ? `/editor?${query}` : "/editor";
    window.open(url, "_blank", "noopener,noreferrer");
  };

  const openProject = async (project) => {
    if (!project?.project_name) {
      toast.error("Project name missing.");
      return;
    }
    const token = localStorage.getItem("token");
    if (!token) {
      navigate("/login");
      return;
    }
    try {
      const res = await fetch(`${apiBase}/projects/activate`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ project_name: project.project_name }),
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `HTTP ${res.status}`);
      }
      const payload = await res.json();
      const projectId = payload?.project?.id ? String(payload.project.id) : null;
      const projectKey = projectId || project.project_name;
      if (projectId) {
        localStorage.setItem("projectId", projectId);
      }
      localStorage.setItem("activeProjectName", project.project_name);
      sessionStorage.setItem(`testify:${projectKey}:existingProject`, "true");
      toast.success(`Activated project: ${project.project_name}`);
      navigate("/input/upload", {
        state: {
          projectName: project.project_name,
          projectId: payload?.project?.id,
          flow: "ocr",
        },
      });
    } catch (err) {
      toast.error(err?.message || "Failed to open project.");
    }
  };

  const downloadProject = async (project) => {
    if (!project?.id) {
      toast.error("Cannot download project: missing identifier.");
      return;
    }
    const token = localStorage.getItem("token");
    if (!token) {
      navigate("/login");
      return;
    }
    try {
      const res = await fetch(`${apiBase}/projects/${project.id}/download`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const safeName = (project.project_name || `project_${project.id}`)
        .trim()
        .replace(/[^\w\-]+/g, "_");
      const link = document.createElement("a");
      link.href = url;
      link.download = `${safeName || "project"}.zip`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      toast.error(err?.message || "Failed to download project.");
    }
  };

  const deleteProject = async (project) => {
    if (!project?.id) {
      toast.error("Missing project id.");
      return;
    }
    const token = localStorage.getItem("token");
    if (!token) {
      navigate("/login");
      return;
    }
    try {
      setDeletingId(project.id);
      const res = await fetch(`${apiBase}/projects/${project.id}`, {
        method: "DELETE",
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `HTTP ${res.status}`);
      }
      setProjects((prev) => prev.filter((p) => p.id !== project.id));
      toast.success(`Deleted project: ${project.project_name}`);
    } catch (err) {
      toast.error(err?.message || "Failed to delete project.");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <AppShell title="Projects" subtitle="Workspace">
      <div className={styles.projectsContainer}>
        <section className={styles.projectsPanel}>
          <div className={styles.projectsHeader}>
            <div>
              <p className={styles.projectsOverline}>All Projects</p>
              <h2 className={styles.projectsTitle}>Recent Projects</h2>
            </div>
            <span className={styles.projectsHint}>
              Total Projects : {sortedProjects.length} 
            </span>
          </div>

          {loading && (
            <div className={styles.stateBanner}>
              <span className="globalSpinner" aria-hidden="true"></span>
              Loading projects...
            </div>
          )}
          {error && <div className={styles.errorBanner}>{error}</div>}

          {!loading && !error && sortedProjects.length === 0 && (
            <div className={styles.emptyState}>
              <h3>No projects yet</h3>
              <p>Create a project to get started.</p>
            </div>
          )}

          {!loading && !error && sortedProjects.length > 0 && (
            <div className={styles.projectsTable}>
              <div className={styles.projectsHeaderRow}>
                <span>Project</span>
                <span>Framework</span>
                <span>Created</span>
                <span>Actions</span>
              </div>
              {sortedProjects.map((project) => (
                <div key={project.id || project.project_name} className={styles.projectsRow}>
                  <div className={styles.projectsCell}>
                    <div className={styles.projectName}>{project.project_name}</div>
                    <div className={styles.projectMeta}>Saved</div>
                  </div>
                  <div className={styles.projectsCell}>
                    {(project.framework || "").trim()} {project.language ? ` / ${project.language}` : ""}
                  </div>
                  <div className={styles.projectsCell}>
                    {project.created_at ? new Date(project.created_at).toLocaleString() : "-"}
                  </div>
                  <div className={styles.projectsCell}>
                    <div className={styles.projectActions}>
                      <button
                        type="button"
                        className={styles.actionButton}
                        onClick={() => openEditor(project)}
                      >
                        Configure
                      </button>
                      <button
                        type="button"
                        className={styles.actionButton}
                        onClick={() => downloadProject(project)}
                      >
                        Download
                      </button>
                      <button
                        type="button"
                        className={styles.actionButton}
                        onClick={() => openReport(project)}
                      >
                        Reports
                      </button>
                      <button
                        type="button"
                        className={styles.executeButton}
                        onClick={() => openProject(project)}
                      >
                        Open
                      </button>
                      <button
                        type="button"
                        className={styles.deleteButton}
                        onClick={() => deleteProject(project)}
                        disabled={deletingId === project.id}
                        aria-label={`Delete ${project.project_name}`}
                      >
                        <i className="fa-solid fa-trash" aria-hidden="true"></i>
                      </button>
                    </div>
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

export default Projects;
