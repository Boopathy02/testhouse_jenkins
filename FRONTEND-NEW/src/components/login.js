import React, { useState, useEffect } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'react-toastify';
import styles from "../css/Login.module.css";
import API_BASE_URL from '../config';

const Login = () => {
    const [organization, setOrganization] = useState('');
    const [email, setEmail] = useState('');
    const [password, setPassword] = useState('');
    const [error, setError] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const [isLightTheme, setIsLightTheme] = useState(false);
    const navigate = useNavigate();

    useEffect(() => {
        const token = localStorage.getItem('token');
        if (token) {
            navigate('/');
        }
    }, [navigate]);

    const handleSubmit = async (e) => {
        e.preventDefault();
        setError('');
        // Use dynamic API base URL (prop -> env -> default)
        try {
            const response = await fetch(`${API_BASE_URL}/login`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    organization,
                    email,
                    password,
                }),
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'Failed to login.');
            }

            const data = await response.json();
            localStorage.setItem('token', data.access_token);
            toast.success('Logged in successfully');
            navigate('/');
        } catch (err) {
            setError(err.message || 'Unexpected error. Please try again.');
            toast.error(err.message || 'Failed to login. Please try again.');
        }
    };

    return (
        <div className={`${styles.container} ${isLightTheme ? styles.lightTheme : ""}`}>
            <button
                type="button"
                className={styles.themeToggle}
                onClick={() => setIsLightTheme((prev) => !prev)}
                aria-label="Toggle light theme"
            >
                <i className={`fa-solid ${isLightTheme ? "fa-moon" : "fa-sun"}`}></i>
            </button>
            <div className={styles.authShell}>
                <aside className={styles.brandPanel}>
                    <div className={styles.brandContent}>
                        <div className={styles.brandBadge}>TA</div>
                        <h1>Testify Automator</h1>
                        <p>Access your Testify Automator workspace to design, generate, and execute AI-driven test suites.</p>
                        <div className={styles.brandMeta}>
                            <span>Project Ready</span>
                            <span>Execution Insights</span>
                            <span>Audit Friendly</span>
                        </div>
                    </div>
                </aside>
                <form onSubmit={handleSubmit} className={styles.form}>
                    <h2>Login</h2>
                    <label>
                        Organization
                        <input
                            type="text"
                            placeholder="Organization"
                            value={organization}
                            onChange={(e) => setOrganization(e.target.value)}
                            required
                        />
                    </label>
                    <label>
                        Email
                        <input
                            type="email"
                            placeholder="name@company.com"
                            value={email}
                            onChange={(e) => setEmail(e.target.value)}
                            required
                        />
                    </label>
                    <label>
                        Password
                        <div className={styles.passwordField}>
                            <input
                                type={showPassword ? "text" : "password"}
                                placeholder="Password"
                                value={password}
                                onChange={(e) => setPassword(e.target.value)}
                                required
                            />
                            <button
                                type="button"
                                className={styles.passwordToggle}
                                onClick={() => setShowPassword((prev) => !prev)}
                                aria-label={showPassword ? "Hide password" : "Show password"}
                            >
                                <i className={`fa-solid ${showPassword ? "fa-eye-slash" : "fa-eye"}`}></i>
                            </button>
                        </div>
                    </label>
                    {error && <p className={styles.errorMessage}>{error}</p>}
                    <button type="submit" className={styles.submitButton}>Login</button>
                    <p className={styles.formFooter}>
                        Need an account? <Link to="/signup">Sign up</Link>
                    </p>
                </form>
            </div>
        </div>
    );
};

export default Login;
