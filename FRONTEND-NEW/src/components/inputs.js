import { useState, useEffect } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import ImageUpload from "./imageuploads";
import ApiUpload from "./apiuploads";
import EtlUpload from "./etlupload";
import EtlValidation from "./etlvalidation";
import EtlExecute from "./etlexecute";
import PageMethods from "./pagemethods";
import ApiMethods from "./apimethods";
import StoryInput from "./storyinput";
import StoryInputApi from "./storyinput_api";
import URLInput from "./urlinput";
import Execute from "./execute"; // OCR/UI flow
import ExecuteApi from "./execute_api"; // API flow
import AppShell from "./AppShell";
import styles from "../css/Inputs.module.css";

const Input = () => {
  const navigate = useNavigate();
  const location = useLocation();
  const projectName = location.state?.projectName;
  const projectId = location.state?.projectId;
  const stateFlow = location.state?.flow;
  const storedFlow = sessionStorage.getItem("inputFlow");
  const storedStartFlow = sessionStorage.getItem("inputStartFlow");
  const flowFromPath = location.pathname.endsWith("/input/url")
    ? "url"
    : location.pathname.endsWith("/input/api") || location.pathname.endsWith("/input/api/execute")
    ? "api"
    : null;
  const flowType = stateFlow || flowFromPath || storedStartFlow || storedFlow;
  const isUrlFlow = flowType === "url";
  const isApiFlow = flowType === "api";
  const isEtlFlow = flowType === "etl";
  
  
  const [currentStep, setCurrentStep] = useState(1);
  const [persistedFiles, setPersistedFiles] = useState([]);
  const [persistedApiFiles, setPersistedApiFiles] = useState([]);
  const [persistedEtlFiles, setPersistedEtlFiles] = useState([]);
  const [pageNames, setPageNames] = useState([]);
  const [testCases, setTestCases] = useState([]);

  useEffect(() => {
    if (projectName) {
      localStorage.setItem("activeProjectName", projectName);
    }
    if (projectId) {
      localStorage.setItem("projectId", String(projectId));
    }
  }, [projectName, projectId]);

  useEffect(() => {
    if (flowType) {
      sessionStorage.setItem("inputFlow", flowType);
    }
  }, [flowType]);


  const stepFromPath = (path) => {
    if (isUrlFlow) {
      if (path.endsWith("/input/url")) return 1;
      if (path.endsWith("/input/methods")) return 2;
      if (path.endsWith("/input/story")) return 3;
      if (path.endsWith("/input/execute")) return 4;
      return 1;
    }
    if (isApiFlow) {
      if (path.endsWith("/input/api")) return 1;
      if (path.endsWith("/input/methods")) return 2;
      if (path.endsWith("/input/story")) return 3;
      if (path.endsWith("/input/api/execute") || path.endsWith("/input/execute")) return 4;
      return 1;
    }
    if (isEtlFlow) {
      if (path.endsWith("/input/upload")) return 1;
      if (path.endsWith("/input/methods")) return 2;
      if (path.endsWith("/input/execute")) return 3;
      return 1;
    }
    if (path.endsWith("/input/upload")) return 1;
    if (path.endsWith("/input/methods")) return 2;
    if (path.endsWith("/input/story")) return 3;
    if (path.endsWith("/input/url")) return 4;
    if (path.endsWith("/input/execute")) return 5;
    return 1;
  };

  const pathFromStep = (step) => {
    if (isUrlFlow) {
      switch (step) {
        case 1:
          return "/input/url";
        case 2:
          return "/input/methods";
        case 3:
          return "/input/story";
        case 4:
          return "/input/execute";
        default:
          return "/input/url";
      }
    }
    if (isApiFlow) {
      switch (step) {
        case 1:
          return "/input/api";
        case 2:
          return "/input/methods";
        case 3:
          return "/input/story";
        case 4:
          return "/input/api/execute";
        default:
          return "/input/api";
      }
    }
    if (isEtlFlow) {
      switch (step) {
        case 1:
          return "/input/upload";
        case 2:
          return "/input/methods";
        case 3:
          return "/input/execute";
        default:
          return "/input/upload";
      }
    }
    switch (step) {
      case 1:
        return "/input/upload";
      case 2:
        return "/input/methods";
      case 3:
        return "/input/story";
      case 4:
        return "/input/url";
      case 5:
        return "/input/execute";
      default:
        return "/input/upload";
    }
  };

  useEffect(() => {
    const nextStep = stepFromPath(location.pathname || "");
    if (nextStep !== currentStep) {
      setCurrentStep(nextStep);
    }
  }, [location.pathname, currentStep]);

  const handleNext = () => {
    const maxStep = isEtlFlow ? 3 : isUrlFlow || isApiFlow ? 4 : 5;
    const nextStep = Math.min(maxStep, currentStep + 1);
    const nextState = flowType
      ? { ...(location.state || {}), flow: flowType }
      : location.state;
    navigate(pathFromStep(nextStep), { state: nextState });
    setCurrentStep(nextStep);
  };

  const handleBack = () => {
    const prevStep = Math.max(1, currentStep - 1);
    const nextState = flowType
      ? { ...(location.state || {}), flow: flowType }
      : location.state;
    navigate(pathFromStep(prevStep), { state: nextState });
    setCurrentStep(prevStep);
  };

  const renderStep = () => {
    if (isUrlFlow) {
      switch (currentStep) {
        case 1:
          return (
            <URLInput
              onBack={handleBack}
              onNext={handleNext}
              apiMode="url"
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 2:
          return (
            <PageMethods
              onBack={handleBack}
              onNext={handleNext}
              pageNames={pageNames}
              setPageNames={setPageNames}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 3:
          return (
            <StoryInput
              onBack={handleBack}
              onNext={handleNext}
              testCases={testCases}
              setTestCases={setTestCases}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 4:
          return <Execute onBack={handleBack} />;
        default:
          return null;
      }
    }
    if (isApiFlow) {
      switch (currentStep) {
        case 1:
          return (
            <ApiUpload
              handleNext={handleNext}
              persistedFiles={persistedApiFiles}
              setPersistedFiles={setPersistedApiFiles}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 2:
          return (
            <ApiMethods
              onBack={handleBack}
              onNext={handleNext}
              pageNames={pageNames}
              setPageNames={setPageNames}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 3:
          return (
            <StoryInputApi
              onBack={handleBack}
              onNext={handleNext}
              testCases={testCases}
              setTestCases={setTestCases}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 4:
          return <ExecuteApi onBack={handleBack} />;
        default:
          return null;
      }
    }
    if (isEtlFlow) {
      switch (currentStep) {
        case 1:
          return (
            <EtlUpload
              handleNext={handleNext}
              persistedFiles={persistedEtlFiles}
              setPersistedFiles={setPersistedEtlFiles}
              projectName={projectName}
              projectId={projectId}
            />
          );
        case 2:
          return (
            <EtlValidation
              onBack={handleBack}
              onNext={handleNext}
              selectedFiles={persistedEtlFiles}
            />
          );
        case 3:
          return <EtlExecute onBack={handleBack} />;
        default:
          return null;
      }
    }

    switch (currentStep) {
      case 1:
        return (
          <ImageUpload
            handleNext={handleNext}
            persistedFiles={persistedFiles}
            setPersistedFiles={setPersistedFiles}
            projectName={projectName}
            projectId={projectId}
          />
        );
      case 2:
        return (
          <PageMethods
            onBack={handleBack}
            onNext={handleNext}
            pageNames={pageNames}
            setPageNames={setPageNames}
            projectName={projectName}
            projectId={projectId}
          />
        );
      case 3:
        return (
          <StoryInput
            onBack={handleBack}
            onNext={handleNext}
            testCases={testCases}
            setTestCases={setTestCases}
            projectName={projectName}
          />
        );
      case 4:
        return <URLInput onBack={handleBack} onNext={handleNext} apiMode="ocr" />;
      case 5:
        return <Execute onBack={handleBack} />;
      default:
        return null;
    }
  };

  const activeProjectName =
    projectName || localStorage.getItem("activeProjectName") || "";

  return (
    <AppShell
      title={isEtlFlow ? "ETL Testing" : "Workflow Builder"}
      subtitle={
        isEtlFlow
          ? "Upload -> Validate -> Execute"
          : "Ingest -> Enrich -> Generate -> Execute"
      }
      contextItems={[{ label: "Project", value: activeProjectName }]}
      actions={
        <button onClick={() => navigate("/")} className={styles.backButton}>
          Back to Dashboard
        </button>
      }
    >
      <div className={styles.inputContainer}>
        {!isEtlFlow && (
          <>
            <div className={styles.wizardHeader}>
              <h2 className={styles.wizardTitle}>Project Setup Workflow</h2>
              <p className={styles.wizardSubtitle}>
                Orchestrate ingestion, story enrichment, generation, and execution in a single pipeline.
              </p>
            </div>

            <div className={styles.stepContainer}>
              <div className={styles.stepItem}>
                <div className={styles.stepIconContainer}>
                  <i className={`fa-solid fa-arrow-up-from-bracket ${styles.stepIcon}`}></i>
                </div>
                <div className={styles.stepTextContainer}>
                  <h2>Ingest UI</h2>
                  <p>Upload screenshots or visual designs for OCR extraction</p>
                </div>
              </div>

              <div className={styles.stepItem}>
                <div className={styles.stepIconContainer}>
                  <i className={`fa-solid fa-diagram-project ${styles.stepIcon}`}></i>
                </div>
                <div className={styles.stepTextContainer}>
                  <h2>Generate Methods</h2>
                  <p>Build page objects and reusable selectors</p>
                </div>
              </div>

              <div className={styles.stepItem}>
                <div className={styles.stepIconContainer}>
                  <i className={`fa-regular fa-message ${styles.stepIcon}`}></i>
                </div>
                <div className={styles.stepTextContainer}>
                  <h2>Enrich Stories</h2>
                  <p>Import Jira stories or define acceptance criteria</p>
                </div>
              </div>

              <div className={styles.stepItem}>
                <div className={styles.stepIconContainer}>
                  <i className={`fa-solid fa-code ${styles.stepIcon}`}></i>
                </div>
                <div className={styles.stepTextContainer}>
                  <h2>Generate Tests</h2>
                  <p>Generate scripts and configure framework outputs</p>
                </div>
              </div>

              <div
                className={styles.stepItem}
                onClick={() => navigate("/prompts", { state: { projectName, projectId } })}
              >
                <div className={styles.stepIconContainer}>
                  <i className={`fa-solid fa-file-pen ${styles.stepIcon}`}></i>
                </div>
                <div className={styles.stepTextContainer}>
                  <h2>Prompt Studio</h2>
                  <p>Customize AI prompts for the selected project</p>
                </div>
              </div>
            </div>
          </>
        )}

        {renderStep()}
      </div>
    </AppShell>
  );
};

export default Input;


