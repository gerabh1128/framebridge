import { COMPONENTES, EQUIPOS } from "./registro";
import type { Componente, Confianza, Equipo } from "./registro";

export type Evidencia = {
  id: string; title: string; source: string; detail: string; at: string; dateOnly?: boolean;
  hosts: Equipo[]; component: Componente; url?: string; reference?: string; confidence?: Confianza;
};
export type Ficha = {
  component: Componente; label?: string; value: string; confidence: Confianza | "na";
  detail: string; evidence?: string; at?: string;
};
export type Nodo = {
  id: Equipo; name: string; tag: string; description: string; system: string;
  accent: "green" | "blue" | "amber" | "purple"; fleet: boolean;
  installed: string; gpu: string; checkedAt?: string; rows: Ficha[];
};
type Inventario = {
  at: string;
  version: string;
  hosts: { id: string; name: string; role: "central" | "node" | "unverified"; ready: boolean }[];
};
type Version = {
  channel: string; scope: string; value: string; confidence: Confianza; note: string; evidence?: string;
};

export let CORTE = "";
export let VERSION_PRODUCCION = "No confirmado";
export const EVIDENCIAS: Evidencia[] = [];
export const NODOS: Nodo[] = [];
export const VERSIONES: Version[] = [];
export const HISTORIAL: { evidence: string; title: string; detail: string }[] = [];

export const evidencia = (id?: string) => EVIDENCIAS.find(item => item.id === id);

export function cargarInventario(data: Inventario) {
  CORTE = data.at;
  VERSION_PRODUCCION = data.version;
  const central = data.hosts.find(host => host.role === "central");
  if (central) {
    EVIDENCIAS.push({
      id: "panel-version", title: "Versión del central", source: "Proceso del panel de FrameBridge",
      detail: "El proceso local declaró la versión indicada. Las versiones del resto de equipos requieren su propia evidencia.",
      at: data.at, hosts: [central.id], component: "installed",
    });
  }
  for (const host of data.hosts) {
    EQUIPOS.push({ id: host.id, name: host.name });
    const isCentral = host.role === "central";
    const rows: Ficha[] = (Object.keys(COMPONENTES) as Componente[]).map(component => ({
      component, value: "No confirmado", confidence: "pending",
      detail: "Registra una verificación de solo lectura y su evidencia cuando esté disponible.",
    }));
    if (isCentral) {
      rows.splice(rows.findIndex(row => row.component === "installed"), 1, {
        component: "installed", value: data.version, confidence: "confirmed",
        detail: "Versión declarada por el proceso del panel local.", evidence: "panel-version", at: data.at,
      });
    }
    if (host.ready) {
      const id = "fleet-" + host.id;
      const detail = "El inventario autenticado del broker indicó un canal disponible en este corte.";
      EVIDENCIAS.push({ id, title: "Canal Fleet disponible", source: "Broker local",
        detail, at: data.at, hosts: [host.id], component: "fleet" });
      rows.splice(rows.findIndex(row => row.component === "fleet"), 1, {
        component: "fleet", value: "Canal disponible", confidence: "confirmed", detail, evidence: id, at: data.at,
      });
    }
    const tag = isCentral ? "Central" : host.role === "node" ? "Nodo enrolado" : "Sin verificar";
    NODOS.push({ id: host.id, name: host.name, tag, description: tag, system: "Por verificar",
      accent: isCentral ? "green" : "blue", fleet: host.ready, gpu: "No confirmado",
      installed: isCentral ? data.version : "No confirmado", checkedAt: isCentral || host.ready ? data.at : undefined, rows });
  }
  VERSIONES.push(
    { channel: "Instalada", scope: "Central", value: data.version, confidence: "confirmed",
      note: "Versión declarada por el proceso del panel.", evidence: "panel-version" },
    ...["APT firmado", "GitHub · rama", "GitHub · Release", "Código privado"].map(channel => ({
      channel, scope: "Referencia independiente", value: "No confirmado", confidence: "pending" as const,
      note: "Pendiente de una lectura verificable de esta fuente.",
    })),
  );
}
