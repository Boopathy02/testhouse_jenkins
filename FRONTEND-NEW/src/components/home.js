import React, { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import Dashboard from "./dashboard";
import { toast } from "react-toastify";
import Editor from "@monaco-editor/react";
import AppShell from "./AppShell";
import styles from "../css/Home.module.css";

const formatLabel = (label = "") =>
  label
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());

const Home = ({ editorOnly = false }) => {
  const navigate = useNavigate();
  const location = useLocation();

  const [showDialog, setShowDialog] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [framework, setFramework] = useState("Playwright");
  const [language, setLanguage] = useState("Python");
  const [userEmail, setUserEmail] = useState("");
  const [userOrganization, setUserOrganization] = useState("");
  const [projects, setProjects] = useState([]);
  const [expandedProjectKey, setExpandedProjectKey] = useState(null);
  const [projectDetails, setProjectDetails] = useState({});
  const [loadingProjectKey, setLoadingProjectKey] = useState(null);
  const [projectFiles, setProjectFiles] = useState({});
  const [openDirectories, setOpenDirectories] = useState({});
  const [selectedFilePaths, setSelectedFilePaths] = useState({});
  const [loadingFileKey, setLoadingFileKey] = useState(null);
  const [activeFile, setActiveFile] = useState(null);
  const [editorValue, setEditorValue] = useState("");
  const [isSavingFile, setIsSavingFile] = useState(false);
  const [isRunningTests, setIsRunningTests] = useState(false);
  const [openFiles, setOpenFiles] = useState({});
  const [editorAutoOpened, setEditorAutoOpened] = useState(false);
  const [startFlow, setStartFlow] = useState("ocr");

  const isEditorOnly = editorOnly || location.pathname === "/editor";
  const editorParams = new URLSearchParams(location.search || "");
  const editorProjectId = editorParams.get("projectId");
  const editorProjectName = editorParams.get("projectName");
  const [showGitDialog, setShowGitDialog] = useState(false);
  const [gitRepoUrl, setGitRepoUrl] = useState("");
  const [gitBaseBranch, setGitBaseBranch] = useState("main");
  const [gitTargetBranch, setGitTargetBranch] = useState("feature/generated-tests");
  const [gitCommitMessage, setGitCommitMessage] = useState("Sync generated tests");
  const [gitUsername, setGitUsername] = useState("");
  const [gitTokenEnv, setGitTokenEnv] = useState("GIT_TOKEN");
  const [gitAuthorName, setGitAuthorName] = useState("");
  const [gitAuthorEmail, setGitAuthorEmail] = useState("");
  const [gitTargetProject, setGitTargetProject] = useState(null);
  const [isPushingToGit, setIsPushingToGit] = useState(false);

  const clearEditorState = () => {
    setActiveFile(null);
    setEditorValue("");
  };

  const apiBase = process.env.REACT_APP_API_URL || "http://127.0.0.1:8001";

  const resolveLanguage = (ext = "") => {
    const map = {
      js: "javascript",
      jsx: "javascript",
      ts: "typescript",
      tsx: "typescript",
      py: "python",
      json: "json",
      html: "html",
      css: "css",
      md: "markdown",
      yml: "yaml",
      yaml: "yaml",
      sh: "shell",
    };
    const normalized = (ext || "").toLowerCase();
    return map[normalized] || normalized || "plaintext";
  };

  const getProjectKey = (project) => {
    if (!project) {
      return "";
    }
    if (project.id !== undefined && project.id !== null) {
      return `id-${project.id}`;
    }
    const slug = (project.project_name || "").trim().toLowerCase();
    return `slug-${slug}`;
  };

  useEffect(() => {
    if (location.state?.needsProject) {
      toast.info("Select or open a project to continue.");
      navigate(".", { replace: true, state: {} });
    }
  }, [location.state, navigate]);

  useEffect(() => {
    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }

    try {
      const payload = JSON.parse(atob(token.split(".")[1]));
      setUserEmail(payload.sub);
      if (payload.org) {
        setUserOrganization(payload.org);
      } else {
        setUserOrganization("");
      }
    } catch (e) {
      console.error("Invalid token:", e);
      handleLogout();
      return;
    }

    const loadProjects = async () => {
      try {
        const res = await fetch(`${apiBase}/projects`, {
          headers: {
            Authorization: `Bearer ${token}`,
          },
        });
        if (res.status === 401) {
          handleLogout();
          return;
        }
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const data = await res.json();
        setProjects(Array.isArray(data?.projects) ? data.projects : []);
      } catch (err) {
        console.error("Failed to load projects:", err);
        setProjects([]);
      }
    };

    loadProjects();
  }, []);

  useEffect(() => {
    if (!location.hash) {
      return;
    }
    const targetId = location.hash.replace("#", "");
    if (!targetId) {
      return;
    }
    const el = document.getElementById(targetId);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [location.hash, projects]);

  useEffect(() => {
    const openFile = location.state?.openFile;
    if (!openFile?.path) {
      return;
    }

    if (!projects.length) {
      return;
    }

    const project =
      openFile.projectId !== undefined && openFile.projectId !== null
        ? projects.find((item) => item.id === openFile.projectId)
        : projects.find((item) => item.project_name === openFile.projectName);

    if (!project) {
      return;
    }

    const projectKey = getProjectKey(project);
    const normalizedPath = String(openFile.path).replace(/^\/+/, "");

    if (expandedProjectKey !== projectKey) {
      handleToggleProject(project, projectKey);
    } else if (!projectFiles?.[projectKey]?.[""]) {
      fetchProjectDirectory(project, projectKey, "");
    }

    fetchProjectFileContent(project, projectKey, normalizedPath);
    navigate(".", { replace: true, state: {} });
  }, [location.state, projects, expandedProjectKey, projectFiles]);

  const handleLogout = () => {
    localStorage.removeItem("token");
    setUserEmail("");
    setUserOrganization("");
    navigate("/login");
  };

  const fetchProjectDirectory = async (project, projectKey, path = "") => {
    if (!project?.id) {
      return;
    }
    const normalizedPath = path || "";
    const existing = projectFiles?.[projectKey]?.[normalizedPath];
    if (existing) {
      return existing;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }

    try {
      setLoadingFileKey(`dir:${projectKey}:${normalizedPath}`);
      const url = new URL(`${apiBase}/projects/${project.id}/files`);
      if (normalizedPath) {
        url.searchParams.set("path", normalizedPath);
      }
      const res = await fetch(url.toString(), {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const data = await res.json();
      const pathKey = data?.path || "";
      setProjectFiles((prev) => ({
        ...prev,
        [projectKey]: {
          ...(prev[projectKey] || {}),
          [pathKey]: Array.isArray(data?.entries) ? data.entries : [],
        },
      }));
      return data;
    } catch (e) {
      console.error("Failed to load project files:", e);
      toast.error(`Failed to load files: ${e.message || e}`);
  } finally {
    setLoadingFileKey(null);
  }
  return null;
  };

  const activeProjectKey = expandedProjectKey;
  const activeProjectDetails = activeProjectKey ? projectDetails[activeProjectKey] : null;
  const activeProject =
    activeProjectDetails?.project ||
    projects.find((proj) => getProjectKey(proj) === activeProjectKey) ||
    null;
  const activePaths = activeProjectDetails?.paths || null;
  const activeFileMap = activeProjectKey ? projectFiles[activeProjectKey] || {} : {};
  const selectedProjectFile = activeProjectKey ? selectedFilePaths[activeProjectKey] || "" : "";
  const isEditorDirty = Boolean(
    activeFile && editorValue !== (activeFile.content ?? "")
  );
  const canSaveFile = Boolean(activeFile?.projectId && selectedProjectFile);

  const renderDirectoryTree = (currentPath = "", depth = 0) => {
    if (!activeProjectKey) {
      return null;
    }

    const pathKey = currentPath || "";
    const entries = activeFileMap[pathKey];

    if (!entries || entries.length === 0) {
      if (pathKey === "" && activeProject?.id) {
        const isRootLoading = loadingFileKey === `dir:${activeProjectKey}:${pathKey}`;
        return (
          <button
            type="button"
            className={styles.projectFileLoadButton}
            onClick={() => fetchProjectDirectory(activeProject, activeProjectKey, "")}
          >
            {isRootLoading ? (
              <>
                <span className="globalSpinner" aria-hidden="true"></span>
                Loading files...
              </>
            ) : (
              "Load Project Files"
            )}
          </button>
        );
      }

      if (pathKey === "") {
        return (
          <p className={styles.projectFileHint}>
            {activeProject
              ? "No generated files yet. Run an extraction or generation workflow to populate this project."
              : "Select a project to explore its generated files."}
          </p>
        );
      }
      return null;
    }

    return (
      <ul className={styles.projectFilesList}>
        {entries.map((entry) => {
          const entryPath = entry.path;
          const isDirectory = entry.type === "directory";
          const entryKey = `${activeProjectKey}::${entryPath || ""}`;

          if (isDirectory) {
            const isOpen = !!openDirectories[entryKey];
            const isDirLoading = loadingFileKey === `dir:${activeProjectKey}:${entryPath}`;
            const handleToggle = () => {
              setOpenDirectories((prev) => ({
                ...prev,
                [entryKey]: !isOpen,
              }));
              if (!isOpen && activeProject?.id) {
                fetchProjectDirectory(activeProject, activeProjectKey, entryPath);
              }
            };

            return (
              <li
                key={entryKey || entry.name}
                className={styles.projectFileItem}
                style={{ marginLeft: depth * 12 }}
              >
                <button
                  type="button"
                  className={styles.projectFileButton}
                  onClick={handleToggle}
                >
                  <span className={styles.projectFileIcon}>{isOpen ? "▾" : "▸"}</span>
                  {entry.name}
                  {isDirLoading && (
                    <span className={styles.projectFileLoading}>
                      <span className="globalSpinner" aria-hidden="true"></span>
                      Loading...
                    </span>
                  )}
                </button>
                {isOpen && renderDirectoryTree(entryPath, depth + 1)}
              </li>
            );
          }

          const isFileLoading = loadingFileKey === `file:${activeProjectKey}:${entryPath}`;
          const isSelected = selectedProjectFile === entryPath;

          return (
            <li
              key={entryPath}
              className={styles.projectFileItem}
              style={{ marginLeft: depth * 12 }}
            >
              <button
                type="button"
                className={`${styles.projectFileButton} ${
                  isSelected ? styles.projectFileButtonActive : ""
                }`}
                onClick={() =>
                  activeProject?.id &&
                  fetchProjectFileContent(activeProject, activeProjectKey, entryPath)
                }
              >
                <span className={styles.projectFileIcon}>•</span>
                {entry.name}
                {isFileLoading && (
                  <span className={styles.projectFileLoading}>
                    <span className="globalSpinner" aria-hidden="true"></span>
                    Loading...
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    );
  };

  const fetchProjectFileContent = async (project, projectKey, path) => {
    if (!project?.id || !path) {
      return;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }

    try {
      setLoadingFileKey(`file:${projectKey}:${path}`);
      const url = new URL(`${apiBase}/projects/${project.id}/files/content`);
      url.searchParams.set("path", path);
      const res = await fetch(url.toString(), {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const fetched = await res.json();
      const language = resolveLanguage(fetched?.language);
      const data = { ...fetched, language };
      const filePayload = {
        projectKey,
        projectId: project.id,
        projectName: project.project_name,
        ...data,
        originalContent: data.content || "",
      };
      setSelectedFilePaths((prev) => ({
        ...prev,
        [projectKey]: data?.path || path,
      }));
      setActiveFile(filePayload);
      setOpenFiles((prev) => {
        const projectEntries = prev[projectKey] || [];
        const existingIdx = projectEntries.findIndex((entry) => entry.path === filePayload.path);
        const nextEntries = [...projectEntries];
        if (existingIdx >= 0) {
          nextEntries[existingIdx] = filePayload;
        } else {
          nextEntries.push(filePayload);
        }
        return {
          ...prev,
          [projectKey]: nextEntries,
        };
      });
      setEditorValue(filePayload.content || "");
      return data;
    } catch (e) {
      console.error("Failed to load project file content:", e);
      toast.error(`Failed to load file: ${e.message || e}`);
    } finally {
      setLoadingFileKey(null);
    }
    return null;
  };

  const fetchProjectTree = async (project, projectKey, path = "") => {
    const data = await fetchProjectDirectory(project, projectKey, path);
    const entries = Array.isArray(data?.entries) ? data.entries : [];
    for (const entry of entries) {
      if (entry.type === "directory") {
        await fetchProjectTree(project, projectKey, entry.path);
      }
    }
  };

  const handleEditorChange = (value) => {
    setEditorValue(value ?? "");
  };

  const handleDiscardEditorChanges = () => {
    if (activeFile) {
      setEditorValue(activeFile.content ?? "");
    }
  };

  const handleSaveActiveFile = async () => {
    if (!canSaveFile || isSavingFile) {
      return;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }

    try {
      setIsSavingFile(true);
      const res = await fetch(`${apiBase}/projects/${activeFile.projectId}/files/content`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          path: selectedProjectFile,
          content: editorValue ?? "",
        }),
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const saved = await res.json();
      setActiveFile((prev) =>
        prev
          ? {
              ...prev,
              content: editorValue ?? "",
              language: resolveLanguage(saved?.language || prev.language),
            }
          : prev
      );
      toast.success(`Saved ${saved?.path || "file"}`);
    } catch (e) {
      console.error("Failed to save file:", e);
      toast.error(`Failed to save file: ${e.message || e}`);
    } finally {
      setIsSavingFile(false);
    }
  };

  const handleRunTests = async () => {
    if (isRunningTests) {
      return;
    }
    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }
    try {
      setIsRunningTests(true);
      const res = await fetch(`${apiBase}/rag/run-generated-story-test`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      let payload = null;
      try {
        payload = await res.json();
      } catch (err) {
        payload = null;
      }
      const status = payload?.status || "Triggered";
      toast.success(`Test run status: ${status}`);
    } catch (e) {
      console.error("Failed to run generated tests:", e);
      toast.error(`Failed to run tests: ${e.message || e}`);
    } finally {
      setIsRunningTests(false);
    }
  };

  const handleStartProject = async () => {
    if (!projectName.trim()) {
      toast.error("Please enter a project name."); // Using toast for better UX
      return;
    }

    // Prevent duplicate project names (client-side)
    const exists = projects.some(
      (p) => (p?.project_name || "").trim().toLowerCase() === projectName.trim().toLowerCase()
    );
    if (exists) {
      toast.error(`Project '${projectName.trim()}' already exists.`);
      return;
    }

    console.log("Project Name:", projectName);
    console.log("Test Framework:", framework);
    console.log("Programming Language:", language);

    let savedProject = null;
    // Send details to backend
    try {
      const token = localStorage.getItem("token");
      if (!token) {
        handleLogout();
        return;
      }
      const res = await fetch(`${apiBase}/projects/save-details`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({ project_name: projectName.trim(), framework, language }),
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const payload = await res.json().catch(() => null);
      savedProject = payload?.project || null;
      if (savedProject?.id) {
        localStorage.setItem("projectId", String(savedProject.id));
      }
      if (savedProject?.project_name) {
        localStorage.setItem("activeProjectName", savedProject.project_name);
      }
      toast.success('Project saved');
      // Optimistically add to local list
      setProjects((prev) => {
        if (savedProject) {
          return [savedProject, ...prev];
        }
        return [{
          organization: userOrganization,
          project_name: projectName.trim(),
          framework,
          language,
          created_at: new Date().toISOString()
        }, ...prev];
      });
    } catch (e) {
      console.error('Failed to save project:', e);
      toast.error(`Failed to save: ${e.message || e}`);
      return;
    }

    setShowDialog(false);
    const storedFlow = sessionStorage.getItem("inputStartFlow");
    const resolvedFlow = storedFlow || startFlow;
    const startPathMap = {
      url: "/input/url",
      api: "/input/api",
      ocr: "/input/upload",
      etl: "/input/upload",
    };
    const startPath = startPathMap[resolvedFlow] || "/input/upload";
    navigate(startPath, {
      state: {
        projectName: projectName,
        projectId: savedProject?.id,
        flow: resolvedFlow,
      },
    });
    setStartFlow("ocr");
    sessionStorage.removeItem("inputStartFlow");
  };

  const handleDeleteProject = async (project) => {
    if (!project?.id) {
      toast.error("Cannot delete project: missing identifier.");
      return;
    }

    const confirmed = window.confirm(`Delete project "${project.project_name}"? This cannot be undone.`);
    if (!confirmed) {
      return;
    }

    const projectKey = getProjectKey(project);

    try {
      const token = localStorage.getItem("token");
      if (!token) {
        handleLogout();
        return;
      }
      const res = await fetch(`${apiBase}/projects/${project.id}`, {
        method: 'DELETE',
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      toast.success(`Deleted project: ${project.project_name}`);
      setProjects((prev) =>
        prev.filter((p) => {
          if (p.id !== undefined && project.id !== undefined) {
            return p.id !== project.id;
          }
          return (p.project_name || "").trim().toLowerCase() !== (project.project_name || "").trim().toLowerCase();
        })
      );
      setProjectDetails((prev) => {
        const next = { ...prev };
        delete next[projectKey];
        return next;
      });
      if (expandedProjectKey === projectKey) {
        setExpandedProjectKey(null);
        clearEditorState();
      }
      if (loadingProjectKey === projectKey) {
        setLoadingProjectKey(null);
      }
    } catch (e) {
      console.error('Failed to delete project:', e);
      toast.error(`Failed to delete project: ${e.message || e}`);
    }
  };

  const handleToggleProject = async (project, explicitKey) => {
    const projectKey = explicitKey || getProjectKey(project);
    if (!projectKey) {
      return;
    }

    if (expandedProjectKey === projectKey) {
      setExpandedProjectKey(null);
      setLoadingFileKey(null);
      setOpenDirectories({});
      setSelectedFilePaths((prev) => {
        const next = { ...prev };
        delete next[projectKey];
        return next;
      });
      clearEditorState();
      return;
    }

    setExpandedProjectKey(projectKey);
    setOpenDirectories({});
    setLoadingFileKey(null);
    clearEditorState();
    setSelectedFilePaths((prev) => {
      const next = { ...prev };
      delete next[projectKey];
      return next;
    });

    if (projectDetails[projectKey]) {
      if (!projectFiles?.[projectKey]?.[""] && project?.id) {
        fetchProjectDirectory(project, projectKey, "");
      }
      return;
    }

    if (!project?.id) {
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: { project },
      }));
      return;
    }

    try {
      setLoadingProjectKey(projectKey);
      const token = localStorage.getItem("token");
      if (!token) {
        handleLogout();
        return;
      }
      const res = await fetch(`${apiBase}/projects/${project.id}`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const data = await res.json();
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: data,
      }));
      fetchProjectDirectory(project, projectKey, "");
    } catch (e) {
      console.error('Failed to load project details:', e);
      toast.error(`Failed to load project details: ${e.message || e}`);
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: { project },
      }));
    } finally {
      setLoadingProjectKey(null);
    }
  };

  const handleCloseEditor = () => {
    if (!expandedProjectKey) {
      return;
    }
    if (isEditorOnly) {
      window.close();
      return;
    }
    handleToggleProject(activeProject || null, expandedProjectKey);
  };

  useEffect(() => {
    if (!isEditorOnly || editorAutoOpened) {
      return;
    }
    if (!projects.length) {
      return;
    }
    const matched = projects.find((p) => {
      if (editorProjectId && String(p.id) === String(editorProjectId)) {
        return true;
      }
      if (editorProjectName) {
        return (p.project_name || "").trim().toLowerCase() === editorProjectName.trim().toLowerCase();
      }
      return false;
    });
    if (matched) {
      handleConfigureProject(matched, getProjectKey(matched));
      setEditorAutoOpened(true);
      return;
    }
    if (editorProjectId || editorProjectName) {
      toast.error("Project not found for editor view.");
      setEditorAutoOpened(true);
    }
  }, [
    isEditorOnly,
    editorAutoOpened,
    projects,
    editorProjectId,
    editorProjectName,
  ]);

  const handleDownloadProject = async (project) => {
    if (!project?.id) {
      toast.error("Cannot download project: missing identifier.");
      return;
    }

    try {
      const token = localStorage.getItem("token");
      if (!token) {
        handleLogout();
        return;
      }
      const res = await fetch(`${apiBase}/projects/${project.id}/download`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
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
      toast.success(`Downloading project: ${project.project_name}`);
    } catch (e) {
      console.error('Failed to download project:', e);
      toast.error(`Failed to download project: ${e.message || e}`);
    }
  };

  const handleConfigureInNewTab = (project) => {
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

  const handleOpenReports = (project) => {
    const rawId =
      project?.id ??
      project?.project_id ??
      project?.projectId ??
      localStorage.getItem("projectId") ??
      null;
    const numericId = rawId !== null ? Number(rawId) : NaN;
    if (!Number.isInteger(numericId)) {
      toast.error("Select a project with a valid id to view reports.");
      return;
    }
    const reportUrl = `${apiBase}/reports/view/${numericId}/`;
    window.open(reportUrl, "_blank", "noopener,noreferrer");
  };

  const handleConfigureProject = async (project, explicitKey) => {
    const projectKey = explicitKey || getProjectKey(project);
    if (!projectKey) {
      return;
    }

    if (expandedProjectKey === projectKey) {
      if (project?.id) {
        await fetchProjectTree(project, projectKey, "");
      }
      return;
    }

    setExpandedProjectKey(projectKey);
    setOpenDirectories({});
    setLoadingFileKey(null);
    clearEditorState();
    setSelectedFilePaths((prev) => {
      const next = { ...prev };
      delete next[projectKey];
      return next;
    });

    if (projectDetails[projectKey]) {
      if (project?.id) {
        await fetchProjectTree(project, projectKey, "");
      }
      return;
    }

    if (!project?.id) {
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: { project },
      }));
      return;
    }

    try {
      setLoadingProjectKey(projectKey);
      const token = localStorage.getItem("token");
      if (!token) {
        handleLogout();
        return;
      }
      const res = await fetch(`${apiBase}/projects/${project.id}`, {
        headers: {
          Authorization: `Bearer ${token}`,
        },
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const data = await res.json();
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: data,
      }));
      await fetchProjectTree(project, projectKey, "");
    } catch (e) {
      console.error("Failed to load project details:", e);
      toast.error(`Failed to load project details: ${e.message || e}`);
      setProjectDetails((prev) => ({
        ...prev,
        [projectKey]: { project },
      }));
    } finally {
      setLoadingProjectKey(null);
    }
  };

  const handlePushProjectToGit = (project) => {
    if (!project) {
      toast.error("Select a project to push.");
      return;
    }
    setGitTargetProject(project);
    setShowGitDialog(true);
  };

  const handleGitDialogClose = () => {
    if (isPushingToGit) {
      return;
    }
    setShowGitDialog(false);
    setGitRepoUrl("");
    setGitBaseBranch("main");
    setGitTargetBranch("feature/generated-tests");
    setGitCommitMessage("Sync generated tests");
    setGitUsername("");
    setGitTokenEnv("GIT_TOKEN");
    setGitAuthorName("");
    setGitAuthorEmail("");
    setGitTargetProject(null);
  };

  const handleGitDialogSubmit = async () => {
    if (!gitTargetProject?.id) {
      toast.error("Missing project identifier.");
      return;
    }
    if (
      !gitRepoUrl.trim() ||
      !gitBaseBranch.trim() ||
      !gitTargetBranch.trim() ||
      !gitCommitMessage.trim() ||
      !gitUsername.trim() ||
      !gitTokenEnv.trim() ||
      !gitAuthorName.trim() ||
      !gitAuthorEmail.trim()
    ) {
      toast.error("All Git fields are required.");
      return;
    }

    if (["main", "master"].includes(gitTargetBranch.trim().toLowerCase())) {
      toast.error("Target branch cannot be main/master.");
      return;
    }

    const token = localStorage.getItem("token");
    if (!token) {
      handleLogout();
      return;
    }

    try {
      setIsPushingToGit(true);
      const res = await fetch(`${apiBase}/projects/${gitTargetProject.id}/git/push`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          repo_url: gitRepoUrl.trim(),
          base_branch: gitBaseBranch.trim(),
          target_branch: gitTargetBranch.trim(),
          git_username: gitUsername.trim(),
          git_token_env: gitTokenEnv.trim(),
          commit_message: gitCommitMessage.trim(),
          author_name: gitAuthorName.trim(),
          author_email: gitAuthorEmail.trim(),
        }),
      });
      if (res.status === 401) {
        handleLogout();
        return;
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => null);
        throw new Error(txt || `Server returned ${res.status}`);
      }
      const payload = await res.json().catch(() => null);
      toast.success(payload?.message || "Project pushed to Git.");
      handleGitDialogClose();
    } catch (e) {
      console.error("Failed to push project to Git:", e);
      toast.error(`Failed to push project: ${e.message || e}`);
    } finally {
      setIsPushingToGit(false);
    }
  };


  const activeProjectName =
    activeProject?.project_name || localStorage.getItem("activeProjectName") || "";

  return (
    <AppShell
      title={isEditorOnly ? "Project Editor" : "Portfolio Overview"}
      subtitle={isEditorOnly ? "Editor" : "Dashboard"}
      compact={isEditorOnly}
      contextItems={[
        { label: "Organization", value: userOrganization || "Unassigned" },
        { label: "User", value: userEmail },
        { label: "Project", value: activeProjectName },
      ]}
      actions={
        <button onClick={handleLogout} className={styles.logoutButton}>
          Logout
        </button>
      }
    >
      <div className={`${styles.homeContainer} ${isEditorOnly ? styles.editorOnly : ""}`}>
        {expandedProjectKey && (
        <section className={styles.ideLayout}>
          <div className={styles.ideToolbar}>
            <div>
              <p className={styles.ideToolbarLabel}>Project editor</p>
              <h3 className={styles.ideToolbarTitle}>
                {activeProject?.project_name || "Generated files"}
              </h3>
              {selectedProjectFile && (
                <p className={styles.ideToolbarPath}>{selectedProjectFile}</p>
              )}
            </div>
            <div className={styles.ideToolbarActions}>
              <button
                type="button"
                className={`${styles.ideActionButton} ${styles.ideActionButtonPrimary}`}
                disabled={!canSaveFile || !isEditorDirty || isSavingFile}
                onClick={handleSaveActiveFile}
              >
                <i className="fa-solid fa-floppy-disk"></i>
                {isSavingFile ? "Saving..." : "Save changes"}
              </button>
              <button
                type="button"
                className={`${styles.ideActionButton} ${styles.ideActionButtonPrimary}`}
                onClick={handleRunTests}
                disabled={isRunningTests}
              >
                <i className="fa-solid fa-play"></i>
                {isRunningTests ? "Running..." : "Run tests"}
              </button>
              <button
                type="button"
                className={styles.ideActionButton}
                disabled={!isEditorDirty || isSavingFile}
                onClick={handleDiscardEditorChanges}
              >
                <i className="fa-solid fa-rotate-left"></i> Discard
              </button>
              <button
                type="button"
                className={`${styles.ideActionButton} ${styles.ideCloseButton}`}
                onClick={handleCloseEditor}
              >
                <i className="fa-solid fa-circle-xmark"></i> Close editor
              </button>
            </div>
          </div>

          <div className={styles.ideWorkspace}>
            <div className={styles.leftPanel}>
              <h4>Project Files</h4>
              {renderDirectoryTree()}
            </div>
            <div className={styles.rightPanel}>
              {activeFile ? (
                <Editor
                  height="100%"
                  language={resolveLanguage(activeFile.language)}
                  value={editorValue}
                  onChange={handleEditorChange}
                  options={{
                    readOnly: false,
                    minimap: { enabled: true },
                    scrollBeyondLastLine: false,
                    fontSize: 14,
                    wordWrap: "on",
                  }}
                />
              ) : (
                <div className={styles.noFileSelected}>
                  <p>Select a file to view its content</p>
                </div>
              )}
            </div>
          </div>
        </section>
      )}

        {!isEditorOnly && (
          <Dashboard
            projects={projects}
            expandedProjectKey={expandedProjectKey}
            loadingProjectKey={loadingProjectKey}
            getProjectKey={getProjectKey}
            hideRecentProjects
            onToggle={handleToggleProject}
            onConfigure={handleConfigureInNewTab}
            onOpen={async (p) => {
              try {
                const token = localStorage.getItem("token");
                if (!token) {
                  handleLogout();
                  return;
                }
                const res = await fetch(`${apiBase}/projects/activate`, {
                  method: 'POST',
                  headers: {
                    'Content-Type': 'application/json',
                    Authorization: `Bearer ${token}`,
                  },
                  body: JSON.stringify({ project_name: p.project_name })
                });
                if (res.status === 401) {
                  handleLogout();
                  return;
                }
                if (!res.ok) {
                  const txt = await res.text().catch(() => null);
                  throw new Error(txt || `Server returned ${res.status}`);
                }
                const payload = await res.json();
                const projectId = payload?.project?.id ? String(payload.project.id) : null;
                const projectKey = projectId || p.project_name;
                if (projectId) {
                  localStorage.setItem('projectId', projectId);
                }
                localStorage.setItem('activeProjectName', p.project_name);
                sessionStorage.setItem(`testify:${projectKey}:existingProject`, "true");
                toast.success(`Activated project: ${p.project_name}`);
                sessionStorage.setItem("inputFlow", "ocr");
                sessionStorage.removeItem("inputStartFlow");
                navigate('/input/upload', {
                  state: {
                    projectName: p.project_name,
                    projectId: payload?.project?.id,
                    flow: "ocr",
                  },
                });
              } catch (e) {
                console.error('Failed to activate project:', e);
                toast.error(`Failed to open project: ${e.message || e}`);
              }
            }}
            onDownload={handleDownloadProject}
            onDelete={handleDeleteProject}
          onPushToGit={handlePushProjectToGit}
          onViewReport={handleOpenReports}
          onOpenProjects={() => navigate("/projects")}
            onStartOCRProject={() => {
              setStartFlow("ocr");
              sessionStorage.setItem("inputStartFlow", "ocr");
              setShowDialog(true);
            }}
            onStartUrlExecution={() => {
              setStartFlow("url");
              sessionStorage.setItem("inputStartFlow", "url");
              setShowDialog(true);
            }}
            onStartApiProject={() => {
              setStartFlow("api");
              sessionStorage.setItem("inputStartFlow", "api");
              setShowDialog(true);
            }}
            onStartEtlProject={() => {
              setStartFlow("etl");
              sessionStorage.setItem("inputStartFlow", "etl");
              setShowDialog(true);
            }}
          />
        )}

      {/* Projects are now displayed inside Dashboard's Recent Projects */}

      {/* Project Setup Dialog */}
        {!isEditorOnly && showDialog && (
        <div className={styles.dialogOverlay}>
          <div className={styles.dialogContent}>
            <h2 className={styles.dialogContentH2}>Create New Project</h2>

            <label className={styles.formLabel}>
              Project Name:
              <input
                type="text"
                value={projectName}
                onChange={(e) => setProjectName(e.target.value)}
                placeholder="Enter project name"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Select Framework:
              <select
                value={framework}
                onChange={(e) => setFramework(e.target.value)}
                className={styles.formSelect}
              >
                <option>Selenium </option>
                <option>Playwright</option>
                <option>Cypress </option>
                <option>Appium </option>
              </select>
            </label>

            <label className={styles.formLabel}>
              Programming Language:
              <select
                value={language}
                onChange={(e) => setLanguage(e.target.value)}
                className={styles.formSelect}
              >
                <option>Python</option>
                <option>Java</option>
                <option>JavaScript</option>
                <option>C#</option>
              </select>
            </label>

            <div className={styles.dialogActions}>
              <button
                onClick={() => setShowDialog(false)}
                className={styles.cancelButton}
              >
                Cancel
              </button>
              <button
                onClick={handleStartProject}
                className={styles.startButton}
              >
                Start Project
              </button>
            </div>
          </div>
        </div>
      )}

        {showGitDialog && (
        <div className={styles.dialogOverlay}>
          <div className={styles.dialogContent}>
            <h2 className={styles.dialogContentH2}>Push Project to Git</h2>

            <label className={styles.formLabel}>
              Repository URL:
              <input
                type="text"
                value={gitRepoUrl}
                onChange={(e) => setGitRepoUrl(e.target.value)}
                placeholder="https://github.com/user/repo.git"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Base Branch:
              <input
                type="text"
                value={gitBaseBranch}
                onChange={(e) => setGitBaseBranch(e.target.value)}
                placeholder="main"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Target Branch:
              <input
                type="text"
                value={gitTargetBranch}
                onChange={(e) => setGitTargetBranch(e.target.value)}
                placeholder="feature/generated-tests"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Git Username:
              <input
                type="text"
                value={gitUsername}
                onChange={(e) => setGitUsername(e.target.value)}
                placeholder="github-username"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Token Env Var (name only):
              <input
                type="text"
                value={gitTokenEnv}
                onChange={(e) => setGitTokenEnv(e.target.value)}
                placeholder="GIT_TOKEN (env var name, not the token)"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Author Name:
              <input
                type="text"
                value={gitAuthorName}
                onChange={(e) => setGitAuthorName(e.target.value)}
                placeholder="Automation Bot"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Author Email:
              <input
                type="email"
                value={gitAuthorEmail}
                onChange={(e) => setGitAuthorEmail(e.target.value)}
                placeholder="automation@example.com"
                className={styles.formInput}
              />
            </label>

            <label className={styles.formLabel}>
              Commit Message:
              <textarea
                rows="3"
                value={gitCommitMessage}
                onChange={(e) => setGitCommitMessage(e.target.value)}
                placeholder="Sync generated tests"
                className={styles.formInput}
              ></textarea>
            </label>

            <div className={styles.dialogActions}>
              <button
                onClick={handleGitDialogClose}
                className={styles.cancelButton}
                disabled={isPushingToGit}
              >
                Cancel
              </button>
              <button
                onClick={handleGitDialogSubmit}
                className={styles.startButton}
                disabled={isPushingToGit}
              >
                {isPushingToGit ? "Pushing..." : "Push to Git"}
              </button>
            </div>
          </div>
        </div>
        )}
      </div>
    </AppShell>
  );
};

export default Home;
