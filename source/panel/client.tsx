import { createRoot } from "react-dom/client";
import { Dashboard } from "./app/dashboard";
import { cargarInventario } from "./app/datos";
import "./app/globals.css";

const mount = document.getElementById("panel");
if (mount) {
  mount.textContent = "Cargando el inventario local…";
  fetch("/api/inventario", { cache: "no-store", credentials: "same-origin" })
    .then(response => {
      if (!response.ok) throw new Error();
      return response.json();
    })
    .then(data => {
      cargarInventario(data);
      createRoot(mount).render(<Dashboard />);
    })
    .catch(() => { mount.textContent = "El inventario local no está disponible. Recarga la página."; });
}
