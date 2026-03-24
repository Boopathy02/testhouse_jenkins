import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'react-toastify';
import styles from '../css/Login.module.css';
import API_BASE_URL from '../config';

const Signup = () => {
    const [formData, setFormData] = useState({
        organization: '',
        email: '',
        password: '',
    });
    const [error, setError] = useState('');
    const [showPassword, setShowPassword] = useState(false);
    const navigate = useNavigate();

    const handleChange = (event) => {
        const { name, value } = event.target;
        setFormData((prev) => ({
            ...prev,
            [name]: value,
        }));
    };

    const handleSubmit = async (event) => {
        event.preventDefault();
        setError('');

        try {
            const response = await fetch(`${API_BASE_URL}/signup`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData),
            });

            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                throw new Error(data.detail || 'Failed to create account.');
            }

            toast.success('Account created! You can log in now.');
            navigate('/login');
        } catch (err) {
            setError(err.message || 'Unexpected error. Please try again.');
            toast.error(err.message || 'Failed to create account. Please try again.');
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.authShell}>
                <aside className={styles.brandPanel}>
                    <div className={styles.brandContent}>
                        <div className={styles.brandBadge}>TA</div>
                        <h1>Boopathy</h1>
                        <p>Launch a new workspace for AI-driven test automation.</p>
                        <div className={styles.brandMeta}>
                            <span>Unified flows</span>
                            <span>Audit ready</span>
                            <span>Precision output</span>
                        </div>
                    </div>
                </aside>
                <form onSubmit={handleSubmit} className={styles.form}>
                    <h2>Sign Up</h2>
                    <label>
                        Organization
                        <input
                            type="text"
                            name="organization"
                            placeholder="Organization"
                            value={formData.organization}
                            onChange={handleChange}
                            required
                        />
                    </label>
                    <label>
                        Email
                        <input
                            type="email"
                            name="email"
                            placeholder="name@company.com"
                            value={formData.email}
                            onChange={handleChange}
                            required
                        />
                    </label>
                    <label>
                        Password
                        <div className={styles.passwordField}>
                            <input
                                type={showPassword ? "text" : "password"}
                                name="password"
                                placeholder="Password"
                                minLength={8}
                                value={formData.password}
                                onChange={handleChange}
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
                    <button type="submit" className={styles.submitButton}>Create Account</button>
                    <p className={styles.formFooter}>
                        Already have an account? <Link to="/login">Login</Link>
                    </p>
                </form>
            </div>
        </div>
    );
};

export default Signup;
