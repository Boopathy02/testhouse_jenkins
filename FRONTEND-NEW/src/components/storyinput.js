import React, { useState, useEffect, useCallback } from "react";
import axios from "axios";
import * as XLSX from "xlsx";
import { toast, ToastContainer } from "react-toastify";
import styles from "../css/StoryInput.module.css";
import API_BASE_URL from "../config";

const TEST_TYPE_OPTIONS = [
  { label: "UI Tests", value: "ui" },
  { label: "Security Tests", value: "security" },
  { label: "Accessibility Tests", value: "accessibility" },
];

const StoryInput = ({ onBack, onNext, testCases, setTestCases, projectName,
  projectId, }) => {
  const [userStoriesInput, setUserStoriesInput] = useState("");
  const [selectedFile, setSelectedFile] = useState(null);
  const [selectedTestTypes, setSelectedTestTypes] = useState([]);

  const [loadingGeneration, setLoadingGeneration] = useState(false);
  const [loadingJira, setLoadingJira] = useState(false);
  const [loadingExcel, setLoadingExcel] = useState(false);
  const [error, setError] = useState("");

  const [showJiraModal, setShowJiraModal] = useState(false);
  const [jiraBaseUrl, setJiraBaseUrl] = useState("");
  const [jiraEmail, setJiraEmail] = useState("");
  const [jiraApiToken, setJiraApiToken] = useState("");
  const [jiraProjectKey, setJiraProjectKey] = useState("");
  const [jiraIssueKey, setJiraIssueKey] = useState("");
  const [jiraIssueKeys, setJiraIssueKeys] = useState("");
  const [jiraJql, setJiraJql] = useState("");
  const [jiraStoryObjects, setJiraStoryObjects] = useState([]);
  const [collapsedCases, setCollapsedCases] = useState({});

  const [showGitModal, setShowGitModal] = useState(false);
  const [repoUrl, setRepoUrl] = useState("");
  const [branchName, setBranchName] = useState("main");
  const [commitMessage, setCommitMessage] = useState("Add automated test cases");
  const [isPushingToGit, setIsPushingToGit] = useState(false);
  const [activeProjectId, setActiveProjectId] = useState(() => projectId || localStorage.getItem("projectId"));
  const [hasGeneratedStories, setHasGeneratedStories] = useState(false);
  const [allowSkipStories, setAllowSkipStories] = useState(false);

  const handleTestTypeChange = (e) => {
    const { value, checked } = e.target;
    setSelectedTestTypes((prev) => {
      if (checked) {
        return [...prev, value];
      }
      return prev.filter((type) => type !== value);
    });
  };

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

  const getStorageKey = (suffix) => {
    const id = projectId || localStorage.getItem("projectId") || "default";
    return `testify:${id}:story:${suffix}`;
  };

  const getExistingProjectKey = () => {
    const id =
      activeProjectId ||
      projectId ||
      localStorage.getItem("projectId") ||
      localStorage.getItem("activeProjectName") ||
      "default";
    return `testify:${id}:existingProject`;
  };

  useEffect(() => {
    try {
      const saved = sessionStorage.getItem(getStorageKey("state"));
      if (saved) {
        const parsed = JSON.parse(saved);
        if (typeof parsed.userStoriesInput === "string") {
          setUserStoriesInput(parsed.userStoriesInput);
        }
        if (Array.isArray(parsed.selectedTestTypes)) {
          setSelectedTestTypes(parsed.selectedTestTypes);
        }
        if (Array.isArray(parsed.testCases)) {
          setTestCases(parsed.testCases);
          if (parsed.testCases.length > 0) {
            setHasGeneratedStories(true);
          }
        }
      }
      const flag = sessionStorage.getItem(getStorageKey("generated"));
      if (flag === "true") {
        setHasGeneratedStories(true);
      }
      const existingFlag =
        sessionStorage.getItem(getStorageKey("existingProject")) ||
        sessionStorage.getItem(getExistingProjectKey());
      if (existingFlag === "true") {
        setAllowSkipStories(true);
      }
    } catch (err) {
      console.warn("Failed to restore story input state:", err);
    }
  }, []);

  useEffect(() => {
    try {
      const payload = {
        userStoriesInput,
        selectedTestTypes,
        testCases,
      };
      sessionStorage.setItem(getStorageKey("state"), JSON.stringify(payload));
      if (Array.isArray(testCases) && testCases.length > 0) {
        sessionStorage.setItem(getStorageKey("generated"), "true");
        setHasGeneratedStories(true);
      }
    } catch (err) {
      console.warn("Failed to persist story input state:", err);
    }
  }, [userStoriesInput, selectedTestTypes, testCases]);

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
      console.error("Failed to activate project before upload:", err);
      toast.error(err.message || "Failed to activate project.");
      return null;
    }
  }, [activeProjectId, projectName]);

  // Fetch test cases from backend
  const fetchTestCases = async () => {
    if ((!userStoriesInput || userStoriesInput.trim() === "") && !selectedFile) {
      setError("Please enter at least one user story or upload a file.");
      return;
    }

    if (selectedTestTypes.length === 0) {
      setError("Select at least one test type.");
      return;
    }

    try {
      setLoadingGeneration(true);
      setError("");

      if (selectedFile) {
        const aggregated = [];
        for (const type of selectedTestTypes) {
          const formData = new FormData();
          formData.append("file", selectedFile);
          formData.append("test_type", type);
          const activeId = await ensureActiveProject();
          if (!activeId) {
            throw new Error("No active project. Please start a project first.");
          }
          const token = localStorage.getItem("token");
          const res = await axios.post(`${API_BASE_URL}/${activeId}/rag/generate-from-story`, formData, {
            headers: {
              "Content-Type": "multipart/form-data",
              Authorization: `Bearer ${token}`,
            },
          });
          if (Array.isArray(res.data?.results)) {
            aggregated.push(...res.data.results);
          }
        }

        setTestCases(aggregated);
        toast.success("Test cases generated successfully.");
      } else {
        const storyObjectsToUse =
          Array.isArray(jiraStoryObjects) && jiraStoryObjects.length ? jiraStoryObjects : null;
        const stories = storyObjectsToUse
          ? storyObjectsToUse
            .map((obj) => obj?.executable_text || "")
            .map((s) => s.trim())
            .filter((s) => s.length > 0)
          : userStoriesInput
            .split("|")
            .map((s) => s.trim())
            .filter((s) => s.length > 0);

        if (stories.length === 0) {
          setError("Please enter at least one valid user story separated by | ");
          setLoadingGeneration(false);
          return;
        }

        const aggregated = [];
        for (const s of stories) {
          const storyObject = storyObjectsToUse
            ? jiraStoryObjects.find((obj) => (obj?.executable_text || "").trim() === s)
            : null;
          for (const type of selectedTestTypes) {
            const params = new URLSearchParams({
              user_story: s,
              test_type: type,
            });
            if (storyObject?.jira_key) {
              params.append("jira_key", storyObject.jira_key);
            }
            if (Array.isArray(storyObject?.acceptance_criteria)) {
              params.append("acceptance_criteria", JSON.stringify(storyObject.acceptance_criteria));
            }
            const activeId = await ensureActiveProject();
            if (!activeId) {
              throw new Error("No active project. Please start a project first.");
            }
            const token = localStorage.getItem("token");
            const res = await axios.post(
              `${API_BASE_URL}/${activeId}/rag/generate-from-story`,
              params, {
              headers: {
                Authorization: `Bearer ${token}`,
              },
            }
            );
            if (Array.isArray(res.data?.results)) {
              aggregated.push(...res.data.results);
            }
          }
        }

        setTestCases(aggregated);
        toast.success(`Generated ${aggregated.length} test case(s) for ${stories.length} stor${stories.length > 1 ? "ies" : "y"}.`);
      }
    } catch (err) {
      console.error(err);
      setError(err.response?.data?.detail || "Error generating test cases.");
    } finally {
      setLoadingGeneration(false);
    }
  };

  // Import from Jira
  const handleJiraImport = async (payload) => {
    setLoadingJira(true);
    try {
      const response = await axios.post(`${API_BASE_URL}/jira/import`, payload);
      const importedStories = response.data?.stories || [];

      if (importedStories.length > 0) {
        if (Array.isArray(response.data?.story_objects) && response.data.story_objects.length) {
          setJiraStoryObjects(response.data.story_objects);
          setUserStoriesInput(
            response.data.story_objects
              .map((obj) => obj?.executable_text || "")
              .filter((text) => text)
              .join(" |\n")
          );
        } else {
          setJiraStoryObjects([]);
          setUserStoriesInput(importedStories.join(" |\n"));
        }
        // join with pipe so UI shows the delimiter clearly
        setSelectedFile(null);
        toast.success("User stories imported from Jira.");
        setShowJiraModal(false);
      } else {
        toast.info("No stories found in Jira.");
      }
    } catch (err) {
      console.error(err);
      toast.error("Failed to import stories from Jira.");
    } finally {
      setLoadingJira(false);
    }
  };

  // Import from Excel: extract 'User Story' from 'User Stories' sheet
  const handleExcelImport = () => {
    setLoadingExcel(true);
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".xlsx, .xls";

    input.onchange = (e) => {
      const file = e.target.files[0];
      if (!file) return;

      const reader = new FileReader();

      reader.onload = (event) => {
        try {
          const data = new Uint8Array(event.target.result);
          const workbook = XLSX.read(data, { type: "array" });

          const userStoriesSheet = workbook.Sheets["User Stories"];
          if (!userStoriesSheet) {
            toast.error("Sheet named 'User Stories' not found.");
            setLoadingExcel(false);
            return;
          }

          const jsonSheet = XLSX.utils.sheet_to_json(userStoriesSheet, { defval: "" });

          const userStoryColKey = jsonSheet.length
            ? Object.keys(jsonSheet[0]).find((k) => k.trim().toLowerCase() === "user story")
            : null;

          if (!userStoryColKey) {
            toast.error("Column 'User Story' not found in 'User Stories' sheet.");
            setLoadingExcel(false);
            return;
          }

          const stories = jsonSheet
            .map((row) => row[userStoryColKey])
            .filter((val) => typeof val === "string" && val.trim().length > 0);

          // Join with pipe so user can see delimiters; fetchTestCases will split on '|'
          setUserStoriesInput(stories.join(" |\n"));
          setSelectedFile(file);
          setJiraStoryObjects([]);
          toast.success("User stories imported from Excel.");
        } catch (err) {
          console.error(err);
          toast.error("Failed to import user stories from Excel.");
        }
        setLoadingExcel(false);
      };

      reader.readAsArrayBuffer(file);
    };

    input.click();
  };

  // Git push feature functions
  const handlePushToGitClick = () => {
    setShowGitModal(true);
  };

  const handleGitModalSubmit = async () => {
    if (!repoUrl.trim() || !branchName.trim() || !commitMessage.trim()) {
      toast.error("All Git fields are required.");
      return;
    }

    setIsPushingToGit(true);
    try {
      const testCasesContent = testCases.map((tc) => tc.auto_testcase);

      const response = await axios.post(`${API_BASE_URL}/git/push-generated-runs`, {
        repo_url: repoUrl,
        branch_name: branchName,
        commit_message: commitMessage,
      });

      if (response.status === 200) {
        toast.success("Test cases pushed to Git successfully!");
        setShowGitModal(false);

        setRepoUrl("");
        setBranchName("main");
        setCommitMessage("Add automated test cases");
      } else {
        toast.error(`Failed to push to Git: ${response.data?.detail || "Unknown error"}`);
      }
    } catch (err) {
      console.error("Git push error:", err);
      toast.error(`Error pushing to Git: ${err.response?.data?.detail || err.message || "Please try again."}`);
    } finally {
      setIsPushingToGit(false);
    }
  };


  const handleGitModalClose = () => {
    setShowGitModal(false);
    setRepoUrl("");
    setBranchName("main");
    setCommitMessage("Add automated test cases");
  };

  const handleJiraModalClose = () => {
    setShowJiraModal(false);
  };

  const toggleCaseCollapse = (index) => {
    setCollapsedCases((prev) => ({
      ...prev,
      [index]: !prev[index],
    }));
  };

  const parseListInput = (value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter((item) => item.length > 0);

  const handleJiraModalSubmit = async () => {
    if (!jiraBaseUrl.trim() || !jiraEmail.trim() || !jiraApiToken.trim() || !jiraProjectKey.trim()) {
      toast.error("Jira base URL, email, API token, and project key are required.");
      return;
    }

    await handleJiraImport({
      base_url: jiraBaseUrl.trim(),
      email: jiraEmail.trim(),
      api_token: jiraApiToken.trim(),
      project_key: jiraProjectKey.trim(),
      // Single-issue import: backend will ignore project/JQL and return one story.
      issue_key: jiraIssueKey.trim() || undefined,
      issue_keys: parseListInput(jiraIssueKeys),
      jql: jiraIssueKey.trim() ? undefined : jiraJql.trim() || undefined,
    });
  };

  return (
    <div className={styles.storyInputContainer}>
      <ToastContainer />
      <div className={styles.contentBox}>
        <h3 className={styles.title}>Import User Stories</h3>
        <p>Add user stories from Jira, Excel, or create them manually</p>

        <div className={styles.importOptions}>
          {/* Manual Entry */}
          <button
            onClick={() => {
              setSelectedFile(null);
              setJiraStoryObjects([]);
            }}
            className={`${styles.optionCard} ${styles.clickable}`}
          >
            <i className={`fa-solid fa-plus ${styles.optionIcon}`}></i>
            <h3 className={styles.optionTitle}>Manual Entry</h3>
            <p className={styles.optionDescription}>Add user stories manually</p>
          </button>

          {/* Jira Import */}
          <button
            onClick={() => setShowJiraModal(true)}
            disabled={loadingJira}
            className={`${styles.optionCard} ${styles.clickable}`}
          >
            <i className={`fa-solid fa-file-import ${styles.optionIcon}`}></i>
            <h3 className={styles.optionTitle}>
              {loadingJira ? <div className={styles.spinner}></div> : "Import from Jira"}
            </h3>
            <p className={styles.optionDescription}>Connect to Jira Instance</p>
          </button>

          {/* Excel Import */}
          <button
            onClick={handleExcelImport}
            disabled={loadingExcel}
            className={`${styles.optionCard} ${styles.clickable}`}
          >
            <i className={`fa-solid fa-file ${styles.optionIcon}`}></i>
            <h3 className={styles.optionTitle}>
              {loadingExcel ? (
                <div className={styles.spinner}></div>
              ) : (
                "Import Excel" + (selectedFile ? ` (${selectedFile.name})` : "")
              )}
            </h3>
            <p className={styles.optionDescription}>Import Excel file</p>
          </button>
        </div>

        {/* Textarea */}
        <textarea
          rows="5"
          cols="60"
          placeholder="Type your user story here... (use | to separate multiple)"
          value={userStoriesInput}
          onChange={(e) => {
            setUserStoriesInput(e.target.value);
            setSelectedFile(null);
            setJiraStoryObjects([]);
          }}
          className={styles.textArea}
        ></textarea>

        {error && <p className={styles.errorText}>{error}</p>}

        <div className={styles.testTypesContainer}>
          <h4 className={styles.testTypesTitle}>Select Test Types to Generate</h4>
          <p className={styles.testTypesHint}>
            No selection is prefilled. Choose only the test suites you currently need.
          </p>
          <div className={styles.checkboxGroup}>
            {TEST_TYPE_OPTIONS.map((option) => (
              <label key={option.value} className={styles.checkboxLabel}>
                <input
                  type="checkbox"
                  value={option.value}
                  checked={selectedTestTypes.includes(option.value)}
                  onChange={handleTestTypeChange}
                />
                {option.label}
              </label>
            ))}
          </div>
        </div>

        {/* Generate */}
        <div className={styles.generateButtonContainer}>
          <button onClick={fetchTestCases} className={styles.generateButton}>
            {loadingGeneration ? <div className={styles.spinner}></div> : "Generate Test Cases"}
          </button>
        </div>

        {/* Results */}
        {Array.isArray(testCases) && testCases.length > 0 && (
          <>
            <div className={styles.gitPushContainer}>
              <button
                onClick={handlePushToGitClick}
                disabled={isPushingToGit}
                className={styles.gitPushButton}
              >
                {isPushingToGit ? <div className={styles.spinner}></div> : "Push to Git"}
              </button>
            </div>

            {testCases.map((tc, idx) => (
              <div key={idx} className={styles.testCaseCard}>
                <div className={styles.testCaseHeader}>
                  <h4 className={styles.testCaseTitle}>Generated Test Case : {idx + 1}</h4>
                  <button
                    type="button"
                    className={styles.collapseButton}
                    onClick={() => toggleCaseCollapse(idx)}
                    aria-expanded={!collapsedCases[idx]}
                  >
                    {collapsedCases[idx] ? "Expand" : "Collapse"}
                    <i className={`fa-solid fa-chevron-${collapsedCases[idx] ? "down" : "up"}`}></i>
                  </button>
                </div>
                {!collapsedCases[idx] && (
                  <table className={styles.testCaseTable}>
                  <thead>
                    <tr>
                      <th>User Story</th>
                      <th>Automated Test Cases</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className={`${styles.testCaseTableTd} ${styles.userStoryCell}`}>
                        {/* Backend returns Prompt with the story; fall back to textarea */}
                        {tc.Prompt || userStoriesInput || tc.manual_testcase || "-"}
                      </td>
                      <td className={`${styles.testCaseTableTd} ${styles.code}`}>
                        <pre>
                          <code>{tc.auto_testcase || "No output generated"}</code>
                        </pre>
                      </td>
                    </tr>
                  </tbody>
                </table>
                )}
              </div>
            ))}
          </>
        )}
      </div>

      {/* Git Modal */}
      {showGitModal && (
        <div className={styles.modalOverlay}>
          <div className={styles.modalContent}>
            <h2>Push to Git</h2>
            <button onClick={handleGitModalClose} className={styles.closeModalButton}>
              <i className="fa-solid fa-xmark"></i>
            </button>
            <label>
              Repository URL:
              <input
                type="text"
                value={repoUrl}
                onChange={(e) => setRepoUrl(e.target.value)}
                placeholder="e.g., https://github.com/user/repo.git"
              />
            </label>
            <label>
              Branch Name:
              <input
                type="text"
                value={branchName}
                onChange={(e) => setBranchName(e.target.value)}
                placeholder="e.g., main or feature/my-tests"
              />
            </label>
            <label>
              Commit Message:
              <textarea
                rows="3"
                value={commitMessage}
                onChange={(e) => setCommitMessage(e.target.value)}
                placeholder="Enter commit message"
              ></textarea>
            </label>
            <div className={styles.modalActions}>
              <button onClick={handleGitModalClose} className={styles.modalCancelButton} disabled={isPushingToGit}>
                Cancel
              </button>
              <button onClick={handleGitModalSubmit} className={styles.modalSubmitButton} disabled={isPushingToGit}>
                {isPushingToGit ? <div className={styles.spinner}></div> : "Push"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Jira Modal */}
      {showJiraModal && (
        <div className={styles.modalOverlay}>
          <div className={styles.modalContent}>
            <h2>Import from Jira</h2>
            <button onClick={handleJiraModalClose} className={styles.closeModalButton}>
              <i className="fa-solid fa-xmark"></i>
            </button>
            <label>
              Jira Base URL:
              <input
                type="text"
                value={jiraBaseUrl}
                onChange={(e) => setJiraBaseUrl(e.target.value)}
                placeholder="e.g., https://your-domain.atlassian.net"
              />
            </label>
            <label>
              Email or Username:
              <input
                type="text"
                value={jiraEmail}
                onChange={(e) => setJiraEmail(e.target.value)}
                placeholder="jira-user@example.com"
              />
            </label>
            <label>
              API Token:
              <input
                type="password"
                value={jiraApiToken}
                onChange={(e) => setJiraApiToken(e.target.value)}
                placeholder="Jira API token"
              />
            </label>
            <label>
              Project Key:
              <input
                type="text"
                value={jiraProjectKey}
                onChange={(e) => setJiraProjectKey(e.target.value)}
                placeholder="e.g., TEST"
              />
            </label>
            <label>
              Jira Issue Key (optional):
              <input
                type="text"
                value={jiraIssueKey}
                onChange={(e) => setJiraIssueKey(e.target.value)}
                placeholder="e.g., BAN-12"
              />
            </label>
            <label>
              JQL (optional):
              <textarea
                rows="3"
                value={jiraJql}
                onChange={(e) => setJiraJql(e.target.value)}
                disabled={Boolean(jiraIssueKey.trim())}
                placeholder='e.g., project = TEST AND issuetype = "Story"'
              ></textarea>
            </label>
            <div className={styles.modalActions}>
              <button onClick={handleJiraModalClose} className={styles.modalCancelButton} disabled={loadingJira}>
                Cancel
              </button>
              <button onClick={handleJiraModalSubmit} className={styles.modalSubmitButton} disabled={loadingJira}>
                {loadingJira ? <div className={styles.spinner}></div> : "Import"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Nav */}
      <div className={styles.navigationButtons}>
        <button onClick={onBack} className={styles.navButton}>
          <i className="fa-solid fa-angle-left"></i>
          Previous
        </button>

        <button
          onClick={onNext}
          disabled={
            (!Array.isArray(testCases) || testCases.length === 0) &&
            !hasGeneratedStories &&
            !allowSkipStories
          }
          className={`${styles.navButton} ${styles.next}`}
        >
          Next <i className="fa-solid fa-angle-right"></i>
        </button>
      </div>
    </div>
  );
};

export default StoryInput;


