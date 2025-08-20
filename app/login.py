import os
import sys
import subprocess
import webbrowser
import tkinter as tk
from tkinter import messagebox
from tkinter import ttk

try:
    from PIL import Image, ImageTk  # pip install pillow
except Exception as e:
    Image = None
    ImageTk = None

# --- Configs ---
APP_PORT = 9000
SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "carbuy_agenda_web.py")
LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "logo_carbuy.png")

# --- Helpers ---
def start_server_if_needed(port: int = APP_PORT) -> bool:
    """Inicia o servidor do coletor se ainda não estiver rodando. Retorna True se OK."""
    # Tenta abrir a porta no navegador (se já estiver no ar, vai responder)
    # Aqui vamos simplesmente iniciar o processo sempre; se já tiver, ele deve falhar ou ficar bloqueado na porta.
    # Em cenários avançados, você pode testar a porta antes.
    try:
        # Usa o mesmo Python da venv atual:
        py = sys.executable
        # Inicia em background
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        subprocess.Popen(
            [py, SERVER_SCRIPT, "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=(os.name != "nt"),
        )
        return True
    except Exception as e:
        messagebox.showerror("Erro", f"Não foi possível iniciar o servidor:\n{e}")
        return False

def on_login():
    user = entry_user.get().strip()
    pwd  = entry_pass.get().strip()
    if not user or not pwd:
        messagebox.showwarning("Atenção", "Preencha usuário e senha.")
        return

    # Autenticação local simples (opcional): você pode remover se quiser permitir qualquer login local.
    if user != "convidado10" or pwd != "convidado10":
        # Se preferir não travar, basta comentar este bloco.
        if not messagebox.askyesno("Continuar?", "Usuário/senha diferentes do padrão.\nDeseja iniciar mesmo assim?"):
            return

    ok = start_server_if_needed(APP_PORT)
    if not ok:
        return

    # Abre navegador apontando para o servidor local
    url = f"http://127.0.0.1:{APP_PORT}"
    try:
        webbrowser.open(url, new=2)
    except Exception:
        pass

    messagebox.showinfo("Sucesso", "Servidor iniciado. O navegador será aberto no Carbuy Coletor.")

# --- UI ---
root = tk.Tk()
root.title("Carbuy Coletor")
root.geometry("440x560")
root.configure(bg="#FF0000")
root.minsize(420, 520)

# fonte padrão
DEFAULT_FONT = ("Segoe UI", 11)
HEADER_FONT = ("Segoe UI", 18, "bold")
LABEL_FONT  = ("Segoe UI", 11, "bold")

# Container central
container = tk.Frame(root, bg="#FF0000")
container.pack(expand=True)

# Logo
if Image and os.path.exists(LOGO_PATH):
    try:
        img = Image.open(LOGO_PATH)
        # Ajuste de tamanho mantendo proporção
        base_w = 220
        w, h = img.size
        if w > base_w:
            ratio = base_w / float(w)
            img = img.resize((base_w, int(h * ratio)), Image.LANCZOS)
        logo_img = ImageTk.PhotoImage(img)
        logo_label = tk.Label(container, image=logo_img, bg="#FF0000")
        logo_label.pack(pady=(24, 8))
    except Exception:
        title_label = tk.Label(container, text="CARBUY", bg="#FF0000", fg="white", font=HEADER_FONT)
        title_label.pack(pady=(24, 8))
else:
    # fallback se não houver PIL ou logo
    title_label = tk.Label(container, text="CARBUY", bg="#FF0000", fg="white", font=HEADER_FONT)
    title_label.pack(pady=(24, 8))

# Título secundário
subtitle = tk.Label(container, text="Coletor", bg="#FF0000", fg="white", font=("Segoe UI", 14))
subtitle.pack(pady=(0, 18))

# Card (fundo escuro) para os inputs
card = tk.Frame(container, bg="#111111", bd=0, highlightthickness=0)
card.pack(padx=20, pady=10, fill="x")

# Usuário
lbl_user = tk.Label(card, text="Usuário", bg="#111111", fg="#e6edf3", font=LABEL_FONT)
lbl_user.pack(anchor="w", padx=16, pady=(16, 4))

entry_user = tk.Entry(card, font=DEFAULT_FONT, justify="center", bg="#1a1a1a", fg="#e6edf3",
                      insertbackground="#e6edf3", relief="flat")
entry_user.pack(fill="x", padx=16, pady=(0, 12), ipady=8)
entry_user.insert(0, "convidado10")

# Senha
lbl_pass = tk.Label(card, text="Senha", bg="#111111", fg="#e6edf3", font=LABEL_FONT)
lbl_pass.pack(anchor="w", padx=16, pady=(4, 4))

entry_pass = tk.Entry(card, font=DEFAULT_FONT, show="*", justify="center", bg="#1a1a1a", fg="#e6edf3",
                      insertbackground="#e6edf3", relief="flat")
entry_pass.pack(fill="x", padx=16, pady=(0, 18), ipady=8)
entry_pass.insert(0, "convidado10")

# Botão estilizado
btn = tk.Button(
    card,
    text="Carbuy Coletor",
    font=("Segoe UI Semibold", 13),
    bg="#000000",
    fg="#ffffff",
    activebackground="#111111",
    activeforeground="#ffffff",
    relief="flat",
    cursor="hand2",
    command=on_login
)
btn.pack(fill="x", padx=16, pady=(0, 20), ipady=10)

# Rodapé
footer = tk.Label(
    root,
    text="Desenvolvido por Bruno Nascimento – MKT Team",
    bg="#FF0000",
    fg="white",
    font=("Segoe UI", 9, "italic")
)
footer.pack(side="bottom", pady=8)

# Enter envia
root.bind("<Return>", lambda e: on_login())

root.mainloop()
