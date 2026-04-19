#!/usr/bin/env python3
"""
Automated Setup and Plot Generator for Continuous Distributions
Run this script to automatically set up venv, install dependencies, and generate plots.
"""

import subprocess
import sys
import os
import venv

def create_venv():
    """Create virtual environment"""
    venv_path = os.path.join(os.getcwd(), "stat_venv")
    if not os.path.exists(venv_path):
        print("📦 Creating virtual environment 'stat_venv'...")
        venv.create(venv_path, with_pip=True)
        print("✅ Virtual environment created!")
    else:
        print("✅ Virtual environment already exists.")
    return venv_path

def get_python_exe(venv_path):
    """Get the Python executable path inside the venv"""
    if sys.platform == "win32":
        python_exe = os.path.join(venv_path, "Scripts", "python.exe")
        pip_exe = os.path.join(venv_path, "Scripts", "pip.exe")
    else:
        python_exe = os.path.join(venv_path, "bin", "python")
        pip_exe = os.path.join(venv_path, "bin", "pip")
    return python_exe, pip_exe

def install_dependencies(pip_exe):
    """Install required packages"""
    print("📚 Installing dependencies (matplotlib, scipy, numpy)...")
    subprocess.run([pip_exe, "install", "--upgrade", "pip"], check=True)
    subprocess.run([pip_exe, "install", "matplotlib", "scipy", "numpy"], check=True)
    print("✅ Dependencies installed!")

def create_plot_script():
    """Create the plot generation script"""
    plot_script = '''"""
Continuous Distribution PDF Plotter
Generates PDF plots for all major continuous distributions every statistics student should know.
"""

import numpy as np
import matplotlib.pyplot as plt
from scipy import stats
import os

# Create output directory
output_dir = "distribution_plots"
os.makedirs(output_dir, exist_ok=True)

print("🎨 Generating distribution plots...")
print("=" * 60)

# Define x ranges
x_normal = np.linspace(-5, 5, 1000)
x_positive = np.linspace(0, 8, 1000)
x_beta = np.linspace(0, 1, 1000)
x_f = np.linspace(0, 5, 1000)
x_lognorm = np.linspace(0, 4, 1000)

# ========== 1. NORMAL DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
params = [(-2, 0.5), (0, 1), (1, 1.5), (0, 2)]
for mu, sigma in params:
    y = stats.norm.pdf(x_normal, mu, sigma)
    plt.plot(x_normal, y, label=f'μ={mu}, σ={sigma}', linewidth=2)
plt.title('Normal (Gaussian) Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/01_normal_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Normal Distribution")

# ========== 2. UNIFORM DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
ranges = [(-2, 2), (0, 1), (0, 3)]
for a, b in ranges:
    y = stats.uniform.pdf(x_normal, a, b-a)
    plt.plot(x_normal, y, label=f'U({a},{b})', linewidth=2)
plt.title('Uniform Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/02_uniform_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Uniform Distribution")

# ========== 3. EXPONENTIAL DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
rates = [0.5, 1, 2]
for rate in rates:
    y = stats.expon.pdf(x_positive, scale=1/rate)
    plt.plot(x_positive, y, label=f'λ={rate} (rate), β={1/rate}', linewidth=2)
plt.title('Exponential Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/03_exponential_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Exponential Distribution")

# ========== 4. GAMMA DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
gamma_params = [(1, 2), (2, 2), (3, 1), (5, 1)]
for k, theta in gamma_params:
    y = stats.gamma.pdf(x_positive, a=k, scale=theta)
    plt.plot(x_positive, y, label=f'α={k}, β={theta}', linewidth=2)
plt.title('Gamma Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/04_gamma_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Gamma Distribution")

# ========== 5. BETA DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
beta_params = [(0.5, 0.5), (2, 5), (5, 2), (2, 2), (3, 3)]
for a, b in beta_params:
    y = stats.beta.pdf(x_beta, a, b)
    plt.plot(x_beta, y, label=f'α={a}, β={b}', linewidth=2)
plt.title('Beta Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/05_beta_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Beta Distribution")

# ========== 6. CHI-SQUARE DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
df_values = [1, 2, 3, 5, 10]
for df in df_values:
    y = stats.chi2.pdf(x_positive, df)
    plt.plot(x_positive, y, label=f'df={df}', linewidth=2)
plt.title('Chi-Square (χ²) Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/06_chisquare_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Chi-Square Distribution")

# ========== 7. STUDENT'S t-DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
df_t = [1, 2, 5, 30, 100]
for df in df_t:
    y = stats.t.pdf(x_normal, df)
    label = f'df={df}' + (' (Cauchy)' if df == 1 else '')
    plt.plot(x_normal, y, label=label, linewidth=2)
y_norm = stats.norm.pdf(x_normal, 0, 1)
plt.plot(x_normal, y_norm, 'k--', label='N(0,1) reference', linewidth=2, alpha=0.7)
plt.title("Student's t-Distribution", fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/07_tdistribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Student's t-Distribution")

# ========== 8. F-DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
f_params = [(1, 1), (2, 5), (5, 2), (10, 10), (20, 20)]
for d1, d2 in f_params:
    y = stats.f.pdf(x_f, d1, d2)
    plt.plot(x_f, y, label=f'd1={d1}, d2={d2}', linewidth=2)
plt.title('F-Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/08_fdistribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ F-Distribution")

# ========== 9. LOG-NORMAL DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
lognorm_params = [(0.25, 1), (0.5, 1), (1, 1), (0.5, 2)]
for s, scale in lognorm_params:
    y = stats.lognorm.pdf(x_lognorm, s, scale=scale)
    label = f'σ={s}, scale={scale}'
    plt.plot(x_lognorm, y, label=label, linewidth=2)
plt.title('Log-Normal Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/09_lognormal_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Log-Normal Distribution")

# ========== 10. WEIBULL DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
weibull_params = [0.5, 1, 1.5, 2, 3]
for c in weibull_params:
    y = stats.weibull_min.pdf(x_positive, c)
    label = f'k={c}' + (' (Exponential)' if c == 1 else '') + (' (Rayleigh)' if c == 2 else '')
    plt.plot(x_positive, y, label=label, linewidth=2)
plt.title('Weibull Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/10_weibull_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Weibull Distribution")

# ========== 11. LAPLACE (DOUBLE EXPONENTIAL) ==========
plt.figure(figsize=(10, 6))
laplace_scales = [0.5, 1, 2]
for scale in laplace_scales:
    y = stats.laplace.pdf(x_normal, scale=scale)
    plt.plot(x_normal, y, label=f'scale={scale}', linewidth=2)
plt.title('Laplace (Double Exponential) Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/11_laplace_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Laplace Distribution")

# ========== 12. LOGISTIC DISTRIBUTION ==========
plt.figure(figsize=(10, 6))
logistic_scales = [0.5, 1, 2]
for scale in logistic_scales:
    y = stats.logistic.pdf(x_normal, scale=scale)
    plt.plot(x_normal, y, label=f'scale={scale}', linewidth=2)
plt.title('Logistic Distribution', fontsize=14, fontweight='bold')
plt.xlabel('x')
plt.ylabel('Probability Density Function f(x)')
plt.legend()
plt.grid(True, alpha=0.3)
plt.savefig(f'{output_dir}/12_logistic_distribution.png', dpi=150, bbox_inches='tight')
plt.close()
print(" ✓ Logistic Distribution")

# ========== SUMMARY ==========
print("=" * 60)
print(f"✅ All plots generated successfully!")
print(f"📁 Output folder: {output_dir}/")
print("\n📊 Generated plots for 12 continuous distributions:")
print(" 1. Normal (Gaussian)")
print(" 2. Uniform")
print(" 3. Exponential")
print(" 4. Gamma")
print(" 5. Beta")
print(" 6. Chi-Square")
print(" 7. Student's t")
print(" 8. F")
print(" 9. Log-Normal")
print(" 10. Weibull")
print(" 11. Laplace (Double Exponential)")
print(" 12. Logistic")
print("\n💡 These are essential distributions every statistics major should know!")
'''
    
    with open("generate_plots.py", "w") as f:
        f.write(plot_script)
    print("✅ Plot generation script created: generate_plots.py")

def run_plot_script(python_exe):
    """Run the plot generation script"""
    print("\n🎨 Running plot generator...")
    print("-" * 60)
    result = subprocess.run([python_exe, "generate_plots.py"], capture_output=False)
    return result.returncode == 0

def main():
    print("=" * 60)
    print("📊 CONTINUOUS DISTRIBUTION PLOT GENERATOR")
    print("For Statistics Students")
    print("=" * 60)
    
    # Step 1: Create virtual environment
    venv_path = create_venv()
    
    # Step 2: Get Python and pip executables
    python_exe, pip_exe = get_python_exe(venv_path)
    
    # Step 3: Install dependencies
    install_dependencies(pip_exe)
    
    # Step 4: Create plot script
    create_plot_script()
    
    # Step 5: Run the plot generator
    success = run_plot_script(python_exe)
    
    if success:
        print("\n" + "=" * 60)
        print("🎉 ALL TASKS COMPLETED SUCCESSFULLY!")
        print("=" * 60)
        print("\n📁 Check the 'distribution_plots' folder for all PDF plots.")
        print("\n🔍 The plots show PDFs of 12 important continuous distributions:")
        print(" • Normal (Bell curve) - Most common distribution")
        print(" • Uniform - Equal probability over interval")
        print(" • Exponential - Waiting times, memoryless property")
        print(" • Gamma - Generalization of exponential")
        print(" • Beta - Probabilities and proportions")
        print(" • Chi-Square - Variance estimation, goodness-of-fit")
        print(" • Student's t - Small sample inference")
        print(" • F - Comparing variances, ANOVA")
        print(" • Log-Normal - Multiplicative processes")
        print(" • Weibull - Reliability engineering, survival analysis")
        print(" • Laplace - Heavy-tailed, robust statistics")
        print(" • Logistic - Logistic regression, growth models")
    else:
        print("\n❌ Error occurred while generating plots. Please check the output above.")

if __name__ == "__main__":
    main()
