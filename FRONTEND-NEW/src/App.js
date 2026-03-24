import './App.css';
import '@fortawesome/fontawesome-free/css/all.min.css';
import 'react-toastify/dist/ReactToastify.css';

import { useEffect } from 'react';
import { BrowserRouter, Route, Routes, Navigate } from 'react-router-dom';
import { ToastContainer, toast } from 'react-toastify';

import Home from './components/home';
import Input from './components/inputs';
import Login from './components/login';
import Signup from './components/signup';
import TestRunner from './components/testrunner';
import Prompts from './components/prompts';
import Reports from './components/reports';
import Projects from './components/projects';
import Settings from './components/settings';
import API_BASE_URL from './config';



const ProtectedRoute = ({ children }) => {
  const token = localStorage.getItem('token');
  return token ? children : <Navigate to="/login" />;
};

const ProjectProtectedRoute = ({ children }) => {
  const token = localStorage.getItem('token');
  if (!token) {
    return <Navigate to="/login" />;
  }
  const projectId = localStorage.getItem('projectId');
  const projectName = localStorage.getItem('activeProjectName');
  return projectId || projectName
    ? children
    : <Navigate to="/" state={{ needsProject: true }} replace />;
};

function App() {
  useEffect(() => {
    let cancelled = false;
    const checkHealth = async () => {
      try {
        const res = await fetch(`${API_BASE_URL}/health`);
        if (!res.ok) {
          throw new Error(`health status ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled && data?.status && data.status !== 'ok') {
          toast.warn('Backend is running but reports degraded health.');
        }
      } catch (err) {
        if (!cancelled) {
          toast.error('Backend health check failed.');
        }
      }
    };
    checkHealth();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <BrowserRouter>
      <Routes>
        <Route path='/login' element={<Login />} />
        <Route path='/signup' element={<Signup />} />
        
        <Route
          path='/'
          element={
            <ProtectedRoute>
              <Home />
            </ProtectedRoute>
          }
        />
        <Route
          path='/projects'
          element={
            <ProtectedRoute>
              <Projects />
            </ProtectedRoute>
          }
        />
        <Route
          path='/settings'
          element={
            <ProtectedRoute>
              <Settings />
            </ProtectedRoute>
          }
        />
        <Route
          path='/editor'
          element={
            <ProtectedRoute>
              <Home editorOnly />
            </ProtectedRoute>
          }
        />
        <Route
          path='/input'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/upload'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/methods'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/story'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/url'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/api'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/api/execute'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
          path='/input/execute'
          element={
            <ProjectProtectedRoute>
              <Input />
            </ProjectProtectedRoute>
          }
        />
        <Route
            path='/test-runner'
            element={
              <ProjectProtectedRoute>
                <TestRunner />
              </ProjectProtectedRoute>
            }
          />
          <Route
            path='/prompts'
            element={
              <ProjectProtectedRoute>
                <Prompts />
              </ProjectProtectedRoute>
            }
          />  
          <Route
            path='/reports'
            element={
              <ProjectProtectedRoute>
                <Reports />
              </ProjectProtectedRoute>
            }
          />

      </Routes>
      <ToastContainer position="top-right" autoClose={4000} newestOnTop closeOnClick pauseOnHover draggable theme="colored" />
    </BrowserRouter>
  );
}

export default App;
