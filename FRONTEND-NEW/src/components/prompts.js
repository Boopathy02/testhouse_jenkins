import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import AppShell from "./AppShell";
import styles from "../css/Prompts.module.css";
import { toast } from "react-toastify";

// A single prompt editor component
const PromptEditor = ({ prompt, onContentChange, onSave, isSaving }) => {
  const isDirty = prompt.content !== prompt.originalContent;

  return (
    <section className={styles.promptCard}>
      <header className={styles.promptHeader}>
        <h3 className={styles.promptTitle}>{prompt.name}</h3>
        <button
          className={styles.saveButton}
          onClick={onSave}
          disabled={isSaving || !isDirty}
        >
          {isSaving ? "Saving..." : "Save Changes"}
        </button>
      </header>
      <textarea
        className={styles.editorTextarea}
        value={prompt.content}
        onChange={onContentChange}
        disabled={isSaving}
        rows={15}
      />
    </section>
  );
};

const Prompts = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const [prompts, setPrompts] = useState([]);
  const [isLoading, setIsLoading] = useState(true);
  const [savingState, setSavingState] = useState({}); // Tracks saving state per prompt
  const [projectId, setProjectId] = useState(() => {
    const stateId = location.state?.projectId;
    return stateId ? String(stateId) : localStorage.getItem("projectId");
  });
  const [projectName] = useState(() => {
    const stateName = location.state?.projectName;
    return stateName || localStorage.getItem("activeProjectName") || localStorage.getItem("projectName");
  });
  const didRetryRef = useRef(false);
  const activeProjectIdRef = useRef(projectId);

  const token = localStorage.getItem("token");
  const apiBase = process.env.REACT_APP_API_URL || "http://127.0.0.1:8001";

  useEffect(() => {
    if (location.state?.projectId) {
      const nextId = String(location.state.projectId);
      localStorage.setItem("projectId", nextId);
      setProjectId(nextId);
      activeProjectIdRef.current = nextId;
    }
    if (location.state?.projectName) {
      localStorage.setItem("activeProjectName", location.state.projectName);
    }
  }, [location.state]);

  const activateProjectByName = useCallback(async (name) => {
    if (!name || !token) {
      return null;
    }
    try {
      const response = await fetch(`${apiBase}/projects/activate`, {
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
        localStorage.setItem("projectId", newId);
        setProjectId(newId);
        activeProjectIdRef.current = newId;
      }
      return newId;
    } catch (error) {
      console.error("ERROR (Frontend): Failed to activate project:", error);
      toast.error(error.message || "Failed to activate project.");
      return null;
    }
  }, [apiBase, token]);

  const fetchAllPromptContent = useCallback(async (promptFiles, targetProjectId) => {
    console.log("DEBUG (Frontend): fetchAllPromptContent - fetching content for:", promptFiles);
    const headers = { Authorization: `Bearer ${token}` };
    const fetchPromises = promptFiles.map(name =>
      fetch(`${apiBase}/projects/${targetProjectId}/files/content?path=prompts/${name}`, { headers })
        .then(res => {
          if (!res.ok) {
            console.error(`ERROR (Frontend): Failed to fetch content for ${name}. Status: ${res.status}`);
            throw new Error(`Failed to fetch ${name}`);
          }
          return res.json();
        })
        .then(data => ({
          name: name,
          content: data.content,
          originalContent: data.content,
        }))
        .catch(error => {
          console.error(`ERROR (Frontend): Error fetching content for ${name}:`, error);
          toast.error(`Error loading prompt ${name}: ${error.message}`);
          return null; // Return null for failed fetches
        })
    );
    const results = await Promise.all(fetchPromises);
    const successfulPrompts = results.filter(p => p !== null);
    console.log("DEBUG (Frontend): fetchAllPromptContent - successfulPrompts:", successfulPrompts);
    return successfulPrompts;
  }, [apiBase, token]);

  useEffect(() => {
    if (!token) {
      console.log("DEBUG (Frontend): No projectId or token. Redirecting.");
      toast.error("No active project or user session. Please start from the dashboard.");
      navigate("/");
      return;
    }
    console.log(`DEBUG (Frontend): Prompts component mounted for projectId: ${projectId}`);

    const fetchPrompts = async () => {
      setIsLoading(true);
      try {
        let activeId = projectId;
        if (!activeId) {
          if (!projectName) {
            toast.error("No active project. Please start from the dashboard.");
            navigate("/");
            return;
          }
          activeId = await activateProjectByName(projectName);
          if (!activeId) {
            return;
          }
        }

        console.log(`DEBUG (Frontend): Fetching prompt list from: ${apiBase}/projects/${activeId}/files?path=prompts`);
        console.log(`DEBUG (Frontend): apiBase: ${apiBase}, projectId: ${activeId}`);
        const listUrl = new URL(`${apiBase}/projects/${activeId}/files`);
        listUrl.searchParams.set("path", "prompts");
        let response = await fetch(listUrl.toString(), {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (response.status === 404 && projectName && !didRetryRef.current) {
          didRetryRef.current = true;
          const refreshedId = await activateProjectByName(projectName);
          if (refreshedId && refreshedId !== activeId) {
            const refreshedUrl = new URL(`${apiBase}/projects/${refreshedId}/files`);
            refreshedUrl.searchParams.set("path", "prompts");
            response = await fetch(refreshedUrl.toString(), {
              headers: { Authorization: `Bearer ${token}` },
            });
            activeId = refreshedId;
          }
        }
        if (!response.ok) {
          console.error(`ERROR (Frontend): Failed to fetch prompt list. Status: ${response.status}`);
          throw new Error("Failed to fetch prompt list.");
        }
        
        const data = await response.json();
        const entries = Array.isArray(data?.entries) ? data.entries : [];
        const promptFiles = entries
          .filter((entry) => entry.type === "file" && entry.name.endsWith(".txt"))
          .map((entry) => entry.name);
        console.log("DEBUG (Frontend): Received prompt files list:", promptFiles);

        if (promptFiles.length > 0) {
          const promptsWithContent = await fetchAllPromptContent(promptFiles, activeId);
          setPrompts(promptsWithContent);
        } else {
          console.log("DEBUG (Frontend): No prompt files found for this project.");
          setPrompts([]);
        }
        activeProjectIdRef.current = activeId;
        if (activeId && activeId !== projectId) {
          localStorage.setItem("projectId", activeId);
          setProjectId(activeId);
        }
      } catch (error) {
        console.error("ERROR (Frontend): Error in fetchPrompts useEffect:", error);
        toast.error(error.message);
      } finally {
        setIsLoading(false);
      }
    };

    fetchPrompts();

    return () => {
      setIsLoading(false);
    };
  }, [projectId, projectName, navigate, fetchAllPromptContent, apiBase, token, activateProjectByName]);

  const handleContentChange = (index, newContent) => {
    setPrompts(prompts.map((p, i) => 
      i === index ? { ...p, content: newContent } : p
    ));
  };

  const handleSave = async (index) => {
    const promptToSave = prompts[index];
    if (!promptToSave) return;
    const targetProjectId = activeProjectIdRef.current || projectId;
    if (!targetProjectId) {
      toast.error("No active project. Please reopen the prompts editor.");
      return;
    }

    setSavingState(prev => ({ ...prev, [promptToSave.name]: true }));
    try {
      console.log(`DEBUG (Frontend): Saving prompt '${promptToSave.name}' with content length: ${promptToSave.content.length}`);
      const response = await fetch(`${apiBase}/projects/${targetProjectId}/files/content`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          path: `prompts/${promptToSave.name}`,
          content: promptToSave.content,
        }),
      });

      if (!response.ok) {
        console.error(`ERROR (Frontend): Failed to save prompt '${promptToSave.name}'. Status: ${response.status}`);
        throw new Error("Failed to save prompt.");
      }

      setPrompts(prompts.map((p, i) =>
        i === index ? { ...p, originalContent: p.content } : p
      ));
      toast.success(`Prompt '${promptToSave.name}' saved successfully!`);
      console.log(`DEBUG (Frontend): Prompt '${promptToSave.name}' saved successfully.`);
    } catch (error) {
      console.error(`ERROR (Frontend): Error saving prompt '${promptToSave.name}':`, error);
      toast.error(error.message);
    } finally {
      setSavingState(prev => ({ ...prev, [promptToSave.name]: false }));
    }
  };

  return (
    <AppShell
      title="Prompt Studio"
      subtitle="Project Prompt Editor"
      contextItems={[{ label: "Project", value: projectName || "Unknown" }]}
      actions={
        <button onClick={() => navigate("/input")} className={styles.backButton}>
          Back to Project
        </button>
      }
    >
      <main className={styles.mainContent}>
        {isLoading ? (
          <div className={styles.placeholder}>
            <span className="globalSpinner" aria-hidden="true"></span>
            Loading prompts...
          </div>
        ) : prompts.length > 0 ? (
          <div className={styles.editorsGrid}>
            {prompts.map((prompt, index) => (
              <PromptEditor
                key={prompt.name}
                prompt={prompt}
                isSaving={savingState[prompt.name]}
                onContentChange={(e) => handleContentChange(index, e.target.value)}
                onSave={() => handleSave(index)}
              />
            ))}
          </div>
        ) : (
          <div className={styles.placeholder}>
            <p>No prompts found for this project.</p>
          </div>
        )}
      </main>
    </AppShell>
  );
};

export default Prompts;
