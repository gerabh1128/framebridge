export type Equipo = string;
export const EQUIPOS: { id: Equipo; name: string }[] = [];

export const COMPONENTES = {
  system: "Sistema",
  gpu: "GPU",
  models: "Modelos locales",
  services: "Servicios",
  mcp: "MCP",
  fleet: "Fleet",
  fbexec: "FBEXEC",
  installed: "Versión instalada",
  apt: "APT firmado",
  github: "GitHub",
  private: "Código privado",
  smtp: "Correo SMTP",
  storage: "Almacenamiento",
  dns: "DNS",
  tls: "Certificados",
  rdp: "Sesiones RDP / Windows",
} as const;

export type Componente = keyof typeof COMPONENTES;
export const CATEGORIAS_INCIDENCIA = ["smtp", "gpu", "storage", "dns", "tls", "rdp"] as const;
export const ACCIONES = { update: "Actualizar", reboot: "Reiniciar", config: "Cambiar configuración" } as const;
export type Accion = keyof typeof ACCIONES;

export const CONFIANZA = {
  confirmed: "Confirmado",
  assumed: "Supuesto",
  pending: "Pendiente",
} as const;
export type Confianza = keyof typeof CONFIANZA;
export const ESTADOS_INCIDENCIA = { open: "Abierta", reviewing: "En revisión", closed: "Cerrada" } as const;
export type EstadoIncidencia = keyof typeof ESTADOS_INCIDENCIA;
export type TipoRegistro = "incident" | "incident_update" | "verification" | "change" | "checklist";

export type Registro = {
  id: string;
  kind: TipoRegistro;
  host: Equipo;
  category: string;
  state: string;
  title: string;
  detail: string;
  evidence: string;
  relatedId: string | null;
  createdAt: string;
};

export const nombreEquipo = (id: Equipo) => EQUIPOS.find(host => host.id === id)?.name ?? id;

export function etiquetaCategoria(key: string) {
  return COMPONENTES[key as Componente] ?? ACCIONES[key as Accion] ?? key;
}

const dateFormatter = new Intl.DateTimeFormat("es-US", {
  timeZone: "America/New_York", day: "2-digit", month: "short", year: "numeric",
});
const timeFormatter = new Intl.DateTimeFormat("es-US", {
  timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hourCycle: "h23",
});

export function fecha(iso?: string | null, hora = true) {
  if (!iso) return "Sin verificación";
  const value = new Date(iso);
  const day = dateFormatter.format(value).replaceAll(".", "").trim();
  return hora ? day + " · " + timeFormatter.format(value) + " ET" : day;
}
