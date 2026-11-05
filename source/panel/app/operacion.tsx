"use client";

import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  ArrowRight, ArrowUpRight, Check, ClipboardCheck, Clock3, FileText, History, ListChecks,
  LockKeyhole, Plus, Search, ShieldCheck, TriangleAlert, X,
} from "lucide-react";
import { EVIDENCIAS, HISTORIAL, evidencia } from "./datos";
import type { Evidencia } from "./datos";
import {
  ACCIONES, CATEGORIAS_INCIDENCIA, COMPONENTES, CONFIANZA, EQUIPOS, ESTADOS_INCIDENCIA,
  etiquetaCategoria, fecha, nombreEquipo,
} from "./registro";
import type { Accion, Componente, Confianza, Equipo, EstadoIncidencia, Registro, TipoRegistro } from "./registro";
import { Encabezado, Sello } from "./ui";

export type Borrador = Pick<Registro, "kind" | "host" | "category" | "state" | "title" | "detail" | "evidence"> & { relatedId?: string };
export type Guardar = (data: Borrador) => Promise<void>;
export type SolicitudFormulario = {
  kind: Exclude<TipoRegistro, "checklist">;
  host: Equipo;
  category?: string;
  incident?: Registro;
  currentState?: string;
};
type AbrirFormulario = (request: SolicitudFormulario) => void;

export function Modal({ title, kicker, children, close }: { title: string; kicker: string; children: ReactNode; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => { dialog.current?.showModal(); }, []);
  return (
    <dialog ref={dialog} className="modal" aria-labelledby="modal-title" onCancel={event => { event.preventDefault(); close(); }}>
      <div className="modal-heading"><div><span className="eyebrow">{kicker}</span><h2 id="modal-title">{title}</h2></div><button className="icon-button" type="button" aria-label="Cerrar" onClick={close}><X size={21} /></button></div>
      {children}
    </dialog>
  );
}

function OpcionesEquipo({ value, onChange, all = false }: { value: string; onChange: (value: string) => void; all?: boolean }) {
  return (
    <select value={value} onChange={event => onChange(event.target.value)}>
      {all && <option value="all">Todos los equipos</option>}
      {EQUIPOS.map(host => <option key={host.id} value={host.id}>{host.name}</option>)}
    </select>
  );
}

export function Formulario({ request, save, close }: { request: SolicitudFormulario; save: Guardar; close: () => void }) {
  const { kind, incident } = request;
  const isIncident = kind === "incident";
  const update = kind === "incident_update";
  const [host, setHost] = useState(request.host);
  const [category, setCategory] = useState(request.category ?? (isIncident ? "smtp" : "system"));
  const [state, setState] = useState<string>(isIncident ? "open" : update ? (request.currentState ?? "open") : "pending");
  const [title, setTitle] = useState(incident?.title ?? "");
  const [detail, setDetail] = useState("");
  const [reference, setReference] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const titleText = isIncident ? "Nueva incidencia" : update ? "Actualizar incidencia" : kind === "change" ? "Registrar cambio histórico" : "Registrar verificación";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving) return;
    setSaving(true);
    setError("");
    try {
      await save({ kind, host, category, state, title, detail, evidence: reference, relatedId: incident?.id });
    } catch (error) {
      setError(error instanceof Error ? error.message : "No se pudo guardar.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={titleText} kicker="BITÁCORA PRIVADA" close={close}>
      <form className="form" onSubmit={submit}>
        {update ? (
          <p className="field-hint">{incident?.title} · {nombreEquipo(host)} · {etiquetaCategoria(category)}</p>
        ) : (
          <div className="field-grid">
            <label>Equipo<OpcionesEquipo value={host} onChange={value => setHost(value as Equipo)} /></label>
            <label>{isIncident ? "Categoría" : "Componente"}<select value={category} onChange={event => setCategory(event.target.value)}>
              {(isIncident ? CATEGORIAS_INCIDENCIA : Object.keys(COMPONENTES)).map(key => <option key={key} value={key}>{etiquetaCategoria(key)}</option>)}
            </select></label>
          </div>
        )}
        {!isIncident && <label>{update ? "Estado de la incidencia" : "Nivel de verificación"}<select value={state} onChange={event => setState(event.target.value)}>
          {Object.entries(update ? ESTADOS_INCIDENCIA : CONFIANZA).map(([key, label]) => <option value={key} key={key}>{label}{!update && key !== "confirmed" ? " · no confirmado" : ""}</option>)}
        </select></label>}
        {!update && <label>Título<input autoFocus value={title} onChange={event => setTitle(event.target.value)} maxLength={100} required placeholder={isIncident ? "Ej. Avisos SMTP en cola" : "Ej. Versión observada tras lectura"} /></label>}
        <label>{isIncident ? "Síntoma e impacto" : update ? "Nota de seguimiento" : "Observación y alcance"}
          <textarea value={detail} onChange={event => setDetail(event.target.value)} required minLength={8} maxLength={2200} rows={4} placeholder="Describe el resultado, su contexto y la hora si corresponde." />
        </label>
        <label>Referencia de evidencia {state === "confirmed" ? "(requerida)" : "(opcional)"}
          <input value={reference} onChange={event => setReference(event.target.value)} maxLength={600} minLength={state === "confirmed" ? 5 : undefined} required={state === "confirmed"} placeholder="Ej. identificador de recibo, ruta de MT o SHA-256" />
        </label>
        <p className="field-hint"><LockKeyhole size={15} /> Guarda solo referencias depuradas y texto sin credenciales.</p>
        {error && <p role="alert" className="form-error">{error}</p>}
        <div className="modal-actions"><button className="button subtle" type="button" onClick={close}>Cancelar</button><button className="button primary" type="submit" disabled={saving}>{saving ? "Guardando…" : isIncident ? "Guardar incidencia" : "Guardar registro"} <ArrowRight size={16} /></button></div>
      </form>
    </Modal>
  );
}

const normal = (text: string) => text.normalize("NFD").replace(/\p{Diacritic}/gu, "").toLowerCase();
const recordLabels: Record<TipoRegistro, string> = {
  incident: "Incidencia", incident_update: "Seguimiento", verification: "Verificación manual", change: "Cambio histórico", checklist: "Checklist",
};

export function Historial({ records, showEvidence, openForm, initialComponent }: {
  records: Registro[]; showEvidence: (item: Evidencia) => void; openForm: AbrirFormulario; initialComponent?: Componente;
}) {
  const [mode, setMode] = useState<"changes" | "evidence" | "manual">(initialComponent ? "evidence" : "changes");
  const [host, setHost] = useState("all");
  const [component, setComponent] = useState(initialComponent ?? "all");
  const [query, setQuery] = useState("");
  useEffect(() => {
    if (initialComponent) { setMode("evidence"); setComponent(initialComponent); }
  }, [initialComponent]);

  const events = mode === "evidence"
    ? EVIDENCIAS.map(item => ({ item, title: item.title, detail: item.detail }))
    : HISTORIAL.flatMap(event => { const item = evidencia(event.evidence); return item ? [{ item, title: event.title, detail: event.detail }] : []; });
  const visible = events.filter(({ item, title, detail }) => (host === "all" || item.hosts.includes(host as Equipo)) && (component === "all" || item.component === component) && normal(title + " " + detail + " " + item.source).includes(normal(query))).sort((a, b) => b.item.at.localeCompare(a.item.at));
  const manual = records.filter(item => (host === "all" || item.host === host) && (component === "all" || item.category === component) && normal(item.title + " " + item.detail + " " + item.evidence).includes(normal(query)));
  return (
    <>
      <Encabezado kicker="EVIDENCIAS Y AUDITORÍA" title="Historial" detail="Revisa cambios documentados, recibos del corte inicial y registros añadidos a la bitácora.">
        <button type="button" className="button subtle" onClick={() => openForm({ kind: "change", host: host === "all" ? EQUIPOS[0].id : host as Equipo, category: component === "all" ? "system" : component })}><Plus size={16} /> Anotar cambio histórico</button>
      </Encabezado>
      <div className="filters history-modes" aria-label="Tipo de historial">
        {([["changes", "Cambios"], ["evidence", "Evidencias"], ["manual", "Registros manuales"]] as const).map(([id, label]) => <button className={"chip " + (mode === id ? "chosen" : "")} type="button" key={id} aria-pressed={mode === id} onClick={() => setMode(id)}>{label}</button>)}
      </div>
      <div className="filter-toolbar">
        <label className="search-field"><Search size={17} /><span className="sr-only">Buscar en el historial</span><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Buscar por hallazgo o fuente" /></label>
        <label><span className="sr-only">Equipo</span><OpcionesEquipo value={host} onChange={setHost} all /></label>
        <label><span className="sr-only">Componente</span><select value={component} onChange={event => setComponent(event.target.value as Componente | "all")}><option value="all">Todos los componentes</option>{Object.entries(COMPONENTES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      </div>
      {mode === "manual" ? (
        <div className="timeline">
          {manual.map(item => <article className="surface timeline-row" key={item.id}><div className="timeline-icon"><History size={19} /></div><div><div className="timeline-meta">{recordLabels[item.kind]} · {nombreEquipo(item.host)} · {etiquetaCategoria(item.category)} · {fecha(item.createdAt)}</div><h2>{item.title}</h2><p className="preserve-text">{item.detail}</p>{item.evidence && <p className="reference">{item.evidence}</p>}</div>{Object.hasOwn(CONFIANZA, item.state) && <Sello confidence={item.state as Confianza} />}</article>)}
          {manual.length === 0 && <div className="surface empty"><History size={24} /><h3>Sin registros para este filtro</h3><p>Las verificaciones, incidencias y revisiones guardadas aparecerán aquí.</p></div>}
        </div>
      ) : (
        <div className="timeline">
          {visible.map(({ item, title, detail }) => <article className="surface timeline-row" key={item.id}><div className="timeline-icon"><FileText size={19} /></div><div><div className="timeline-meta">{fecha(item.at, !item.dateOnly)} · {item.source}</div><h2>{title}</h2><p>{detail}</p><div className="timeline-tags">{item.hosts.map(key => <span key={key}>{nombreEquipo(key)}</span>)}<span>{COMPONENTES[item.component]}</span></div></div><button className="text-button" type="button" onClick={() => showEvidence(item)}>Ver evidencia <ArrowUpRight size={15} /></button></article>)}
          {visible.length === 0 && <div className="surface empty"><Search size={24} /><h3>Sin coincidencias</h3><p>Ajusta los filtros o el texto de búsqueda.</p></div>}
        </div>
      )}
    </>
  );
}

export function Incidencias({ records, openForm }: { records: Registro[]; openForm: AbrirFormulario }) {
  const [host, setHost] = useState("all");
  const [category, setCategory] = useState("all");
  const [state, setState] = useState("all");

  const tickets = records.filter(row => row.kind === "incident").map(incident => {
    const updates = records.filter(row => row.kind === "incident_update" && row.relatedId === incident.id);
    return { incident, updates, current: (updates[0]?.state ?? incident.state) as EstadoIncidencia };
  }).filter(({ incident, current }) => (host === "all" || host === incident.host) && (category === "all" || category === incident.category) && (state === "all" || state === current));

  return (
    <>
      <Encabezado kicker="BITÁCORA OPERATIVA" title="Incidencias" detail="Documenta síntomas, evidencias y seguimiento dentro del Site.">
        <button className="button primary" type="button" onClick={() => openForm({ kind: "incident", host: host === "all" ? EQUIPOS[0].id : host as Equipo, category: category === "all" ? undefined : category })}><Plus size={17} /> Registrar incidencia</button>
      </Encabezado>
      <div className="incident-categories" aria-label="Categorías de incidencia">{CATEGORIAS_INCIDENCIA.map(key => <button key={key} className={"chip " + (category === key ? "chosen" : "")} aria-pressed={category === key} type="button" onClick={() => setCategory(category === key ? "all" : key)}>{COMPONENTES[key]}</button>)}</div>
      <div className="filter-toolbar"><label><span className="sr-only">Filtrar por equipo</span><OpcionesEquipo value={host} onChange={setHost} all /></label><label><span className="sr-only">Filtrar por estado</span><select value={state} onChange={event => setState(event.target.value)}><option value="all">Todos los estados</option>{Object.entries(ESTADOS_INCIDENCIA).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div>
      <div className="timeline">
        {tickets.map(({ incident, updates, current }) => (
          <article className="surface ticket" key={incident.id}>
            <div className="ticket-top"><div><div className="timeline-meta">{nombreEquipo(incident.host)} · {etiquetaCategoria(incident.category)} · {fecha(incident.createdAt)}</div><h2>{incident.title}</h2></div><span className={"badge incident-" + current}>{ESTADOS_INCIDENCIA[current]}</span></div>
            <p className="preserve-text">{incident.detail}</p>
            {incident.evidence && <p className="reference">{incident.evidence}</p>}
            {updates.length > 0 && <details className="ticket-history"><summary>Seguimiento ({updates.length})</summary>{updates.map(item => <div key={item.id}><small>{fecha(item.createdAt)} · {ESTADOS_INCIDENCIA[item.state as EstadoIncidencia]}</small><p className="preserve-text">{item.detail}</p>{item.evidence && <p className="reference">{item.evidence}</p>}</div>)}</details>}
            <button className="text-button" type="button" onClick={() => openForm({ kind: "incident_update", host: incident.host, category: incident.category, incident, currentState: current })}>Añadir seguimiento <ArrowRight size={15} /></button>
          </article>
        ))}
        {tickets.length === 0 && <div className="surface empty"><TriangleAlert size={28} /><h3>Sin incidencias para este filtro</h3><p>Los avisos de la red se registran manualmente en esta vista.</p></div>}
      </div>
    </>
  );
}

const PLANES = {
  update: {
    steps: [
      "Confirma equipo, versión instalada y servicios que dependen de ella.",
      "Verifica el canal oficial, firma, SHA-256 y compatibilidad con el perfil.",
      "Revisa cargas de trabajo, colas y efecto de la ventana de mantenimiento.",
      "Comprueba un respaldo recuperable y la versión exacta de reversión.",
    ],
    gated: ["Instalar, retirar o degradar un paquete.", "Actualizar repositorios, drivers o kernel.", "Reiniciar servicios o equipos como parte de la instalación."],
  },
  reboot: {
    steps: [
      "Comprueba identidad del equipo, servicios críticos y última hora de arranque.",
      "Verifica sesiones, procesos de inferencia, transferencias y colas pendientes.",
      "Confirma una ruta de recuperación y acceso tras el arranque.",
      "Documenta el respaldo aplicable, la ventana y las pruebas posteriores.",
    ],
    gated: ["Cerrar sesiones, trabajos o servicios.", "Reiniciar o apagar un equipo.", "Modificar tareas de arranque o políticas de inicio."],
  },
  config: {
    steps: [
      "Registra la configuración vigente y el motivo del cambio.",
      "Identifica dependencias, impacto y diferencia propuesta.",
      "Comprueba un respaldo de la configuración anterior y cómo restaurarlo.",
      "Define la ventana, el criterio de aceptación y las comprobaciones posteriores.",
    ],
    gated: ["Editar archivos, permisos o políticas.", "Aplicar cambios de DNS, TLS, firewall, SSH o almacenamiento.", "Reiniciar o recargar servicios para aplicar la configuración."],
  },
};

export function Checklist({ save }: { save: Guardar }) {
  const [host, setHost] = useState<Equipo>(EQUIPOS[0].id);
  const [action, setAction] = useState<Accion>("update");
  const [checked, setChecked] = useState<number[]>([]);
  const [impact, setImpact] = useState("");
  const [backup, setBackup] = useState("");
  const [rollback, setRollback] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const plan = PLANES[action];
  const complete = checked.length === plan.steps.length;
  const percent = Math.round(checked.length / plan.steps.length * 100);

  function resetReview() {
    setChecked([]); setImpact(""); setBackup(""); setRollback(""); setError(""); setSaved(false);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (saving || !complete) return;
    setSaving(true); setError(""); setSaved(false);
    try {
      await save({
        kind: "checklist", host, category: action, state: "reviewed",
        title: "Revisión previa · " + ACCIONES[action] + " · " + nombreEquipo(host),
        detail: "Impacto y ventana\n" + impact.trim() + "\n\nReversión y comprobaciones\n" + rollback.trim() + "\n\nRevisión manual completa (" + checked.length + "/" + plan.steps.length + ").",
        evidence: backup.trim(),
      });
      setSaved(true);
    } catch (error) {
      setError(error instanceof Error ? error.message : "No se pudo guardar la revisión.");
    } finally { setSaving(false); }
  }

  return (
    <>
      <Encabezado kicker="CONTROL DE CAMBIOS" title="Checklist guiada" detail="Prepara una actualización, un reinicio o un cambio de configuración con evidencia, impacto y reversión." />
      <div className="surface plan-select">
        <div><span className="eyebrow">PASO 1</span><h2>Define el alcance</h2></div>
        <label>Equipo<OpcionesEquipo value={host} onChange={value => { setHost(value as Equipo); resetReview(); }} /></label>
        <label>Acción prevista<select value={action} onChange={event => { setAction(event.target.value as Accion); resetReview(); }}>{Object.entries(ACCIONES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      </div>
      <div className="plan-grid section">
        <section className="surface plan-panel">
          <div className="plan-header"><span className="plan-icon green"><ListChecks size={22} /></span><div><span className="eyebrow">PASO 2</span><h2>Solo lectura</h2><p>Marca cada revisión después de comprobar su evidencia.</p></div></div>
          <div className="progress-line"><span>{checked.length} de {plan.steps.length} revisiones</span><strong>{percent}%</strong></div>
          <progress value={checked.length} max={plan.steps.length} aria-label="Progreso de la revisión" />
          <div className="check-items">{plan.steps.map((step, index) => <label key={step}><input type="checkbox" checked={checked.includes(index)} onChange={event => { setSaved(false); setChecked(list => event.target.checked ? [...list, index] : list.filter(item => item !== index)); }} /><span>{step}</span></label>)}</div>
        </section>
        <section className="surface plan-panel gated">
          <div className="plan-header"><span className="plan-icon amber"><LockKeyhole size={22} /></span><div><span className="eyebrow">PUERTA DE AUTORIZACIÓN</span><h2>Requiere autorización</h2><p>Estas acciones se solicitan por separado a Gerardo.</p></div></div>
          <ul className="gated-list">{plan.gated.map(item => <li key={item}><LockKeyhole size={15} /><span>{item}</span></li>)}</ul>
          <p className="gated-note">El Site guarda la preparación. Cualquier ejecución se realiza fuera del panel con autorización expresa.</p>
        </section>
      </div>
      <section className="surface plan-panel section">
        <div className="plan-header"><span className="plan-icon green"><ClipboardCheck size={22} /></span><div><span className="eyebrow">PASO 3</span><h2>Registra la revisión</h2><p>Deja un recibo de preparación en el historial.</p></div></div>
        <form className="form checklist-form" onSubmit={submit}>
          <div className="field-grid">
            <label>Impacto y ventana<textarea value={impact} onChange={event => { setImpact(event.target.value); setSaved(false); }} required minLength={8} maxLength={900} rows={3} placeholder="Servicios afectados, dependencias y momento previsto." /></label>
            <label>Reversión y validación<textarea value={rollback} onChange={event => { setRollback(event.target.value); setSaved(false); }} required minLength={8} maxLength={900} rows={3} placeholder="Cómo revertir y qué comprobar al finalizar." /></label>
          </div>
          <label>Evidencia del respaldo<input required minLength={5} maxLength={600} value={backup} onChange={event => { setBackup(event.target.value); setSaved(false); }} placeholder="Referencia y fecha de la copia, o justificación cuando no aplique." /></label>
          <div className="checklist-actions">
            <span className="field-hint">{saved ? <><Check size={16} /> Revisión guardada en el historial.</> : complete ? <><ShieldCheck size={16} /> Revisión lista para registrar.</> : <><Clock3 size={16} /> Completa las comprobaciones de solo lectura.</>}</span>
            <button type="submit" className="button primary" disabled={!complete || saved || saving}>{saving ? "Guardando…" : "Guardar revisión"} <ArrowRight size={16} /></button>
          </div>
          {error && <p className="form-error" role="alert">{error}</p>}
        </form>
      </section>
    </>
  );
}
