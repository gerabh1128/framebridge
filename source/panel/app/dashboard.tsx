"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity, ArrowLeft, ArrowRight, ArrowUpRight, CalendarDays, Check, ChevronRight,
  ClipboardCheck, Clock3, Cpu, Database, ExternalLink, FileText, GitBranch,
  History, Laptop, Layers3, LayoutDashboard, LockKeyhole, Monitor, Network, Plus,
  RefreshCw, Server, ShieldCheck, TriangleAlert, X,
} from "lucide-react";
import { CORTE, EVIDENCIAS, NODOS, VERSIONES, VERSION_PRODUCCION, evidencia } from "./datos";
import type { Evidencia, Ficha, Nodo } from "./datos";
import { Checklist, Formulario, Historial, Incidencias, Modal } from "./operacion";
import type { Borrador, Guardar, SolicitudFormulario } from "./operacion";
import { COMPONENTES, EQUIPOS, etiquetaCategoria, fecha } from "./registro";
import { Encabezado, Sello } from "./ui";
import type { Componente, Confianza, Equipo, Registro } from "./registro";

const menus = [
  { id: "resumen", label: "Resumen", icon: LayoutDashboard },
  { id: "equipos", label: "Equipos", icon: Server },
  { id: "versiones", label: "Versiones", icon: GitBranch },
  { id: "historial", label: "Historial", icon: History },
  { id: "incidencias", label: "Incidencias", icon: TriangleAlert },
  { id: "checklist", label: "Checklist", icon: ClipboardCheck },
] as const;

type Vista = (typeof menus)[number]["id"];
type Destino = { view: Vista; host?: Equipo; component?: Componente };
type Navegar = (view: Vista, host?: Equipo, component?: Componente) => void;
type Cursor = { before: string; id: string };
type JournalPage = { entries?: Registro[]; next?: Cursor | null; error?: string };

const deviceIcon = (node: Nodo) => node.tag === "Central" ? Server : Monitor;

function leerRuta(): Destino {
  const [view, host, component] = window.location.hash.replace(/^#/, "").split("/");
  return {
    view: menus.find(item => item.id === view)?.id ?? "resumen",
    host: EQUIPOS.find(item => item.id === host)?.id,
    component: Object.hasOwn(COMPONENTES, component ?? "") ? component as Componente : undefined,
  };
}

function Marca() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <svg viewBox="0 0 40 40" fill="none">
        <path d="M7 30h26M10 30V16l10-5 10 5v14M10 22l10-5 10 5M20 17v13M15 30v-4m10 4v-4" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    </span>
  );
}

function Ilustracion() {
  return (
    <svg className="hero-graphic" viewBox="0 0 435 220" fill="none" aria-hidden="true">
      <defs>
        <pattern id="hero-grid" width="27" height="27" patternUnits="userSpaceOnUse">
          <path d="M27 0H0v27" stroke="#42685a" strokeWidth=".7" opacity=".48" />
        </pattern>
      </defs>
      <path d="M28 0h407v220H28z" fill="url(#hero-grid)" />
      <path d="M114 111h81m54 0h82m-110-27V58l-48-25m48 105v30l59 25m-24-85 50-55h44" stroke="#689381" strokeWidth="1.15" strokeDasharray="4 6" />
      <circle cx="221" cy="111" r="70" stroke="#497664" opacity=".56" />
      <circle cx="221" cy="111" r="93" stroke="#497664" opacity=".36" />
      <circle cx="221" cy="111" r="51" stroke="#79ab94" opacity=".6" />
      <rect x="198" y="88" width="46" height="46" rx="11" fill="#baf1d6" />
      <path d="M207 124h28m-24 0v-13l10-6 10 6v13m-20-7 10-6 10 6m-10-6v13" stroke="#15513d" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
      <g fill="#15372d" stroke="#80b69d">
        <rect x="100" y="97" width="28" height="28" rx="7" />
        <rect x="156" y="19" width="28" height="28" rx="7" />
        <rect x="317" y="97" width="28" height="28" rx="7" />
        <rect x="269" y="179" width="28" height="28" rx="7" />
        <rect x="347" y="39" width="28" height="28" rx="7" />
      </g>
      <g fill="#b5f0d3">
        <circle cx="114" cy="111" r="3.6" /><circle cx="170" cy="33" r="3.6" />
        <circle cx="331" cy="111" r="3.6" /><circle cx="283" cy="193" r="3.6" />
      </g>
      <circle cx="361" cy="53" r="3.6" fill="#efcb85" />
    </svg>
  );
}

function EstadoFleet({ node }: { node: Nodo }) {
  return <span className={"fleet-pill " + (node.fleet ? "seen" : "unseen")}><i />{node.fleet ? "Fleet respondió" : "Fuera del inventario Fleet"}</span>;
}

function TarjetaEquipo({ node, navigate }: { node: Nodo; navigate: Navegar }) {
  const Icon = deviceIcon(node);
  return (
    <button type="button" className={"device-card accent-" + node.accent} onClick={() => navigate("equipos", node.id)}>
      <div className="device-card-top">
        <span className="device-icon"><Icon size={23} strokeWidth={1.7} aria-hidden="true" /></span>
        <span className="device-arrow"><ArrowUpRight size={18} aria-hidden="true" /></span>
      </div>
      <span className="device-tag">{node.tag}</span>
      <strong className="device-title">{node.name}</strong>
      <span className="device-description">{node.system}</span>
      <span className="device-values">
        <span><span>FrameBridge</span><strong>{node.installed}</strong></span>
        <span><span>GPU</span><strong>{node.gpu}</strong></span>
      </span>
      <span className="device-card-foot"><EstadoFleet node={node} /></span>
    </button>
  );
}

function TituloSeccion({ title, detail, children }: { title: string; detail?: string; children?: React.ReactNode }) {
  return <div className="section-heading"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div>{children}</div>;
}

function Resumen({ navigate, showEvidence, records, journalReady }: { navigate: Navegar; showEvidence: (e: Evidencia) => void; records: Registro[]; journalReady: boolean }) {
  const fleetCount = NODOS.filter(node => node.fleet).length;
  const openCount = records.filter(record => record.kind === "incident").filter(incident => records.find(record => record.kind === "incident_update" && record.relatedId === incident.id)?.state !== "closed").length;
  return (
    <>
      <section className="hero">
        <Ilustracion />
        <div className="hero-content">
          <span className="hero-kicker"><span /> PANEL DE OPERACIONES</span>
          <h1>Centro de supervisión</h1>
          <p>Estado, versiones y evidencias de FrameBridge en el laboratorio.</p>
          <div className="hero-meta"><CalendarDays size={16} /> Corte inicial · {fecha(CORTE)}</div>
        </div>
      </section>

      <div className="metrics" aria-label="Indicadores del último corte">
        <button type="button" className="metric" onClick={() => navigate("equipos")}>
          <span className="metric-icon"><Layers3 size={19} /></span><span><span className="metric-label">Equipos en inventario</span><strong>{NODOS.length}</strong><small>Infraestructura del laboratorio</small></span>
        </button>
        <button type="button" className="metric" onClick={() => navigate("historial", undefined, "fleet")}>
          <span className="metric-icon green"><Network size={19} /></span><span><span className="metric-label">Respuestas de Fleet</span><strong>{fleetCount}<em> / {NODOS.length}</em></strong><small>Observadas en el corte</small></span>
        </button>
        <button type="button" className="metric" onClick={() => navigate("versiones")}>
          <span className="metric-icon blue"><GitBranch size={19} /></span><span><span className="metric-label">Versión del central</span><strong>{VERSION_PRODUCCION}</strong><small>Leída en el panel local</small></span>
        </button>
        <button type="button" className="metric" onClick={() => navigate("incidencias")}>
          <span className="metric-icon amber"><TriangleAlert size={19} /></span><span><span className="metric-label">Incidencias abiertas</span><strong>{journalReady ? String(openCount).padStart(2, "0") : "—"}</strong><small>Registros manuales del Site</small></span>
        </button>
      </div>

      <section className="section">
        <TituloSeccion title="Equipos" detail="Selecciona un equipo para abrir su ficha y comprobar el alcance de cada lectura.">
          <button className="button subtle" type="button" onClick={() => navigate("equipos")}>Ver inventario <ArrowRight size={16} /></button>
        </TituloSeccion>
        <div className="devices-grid">{NODOS.map(node => <TarjetaEquipo key={node.id} node={node} navigate={navigate} />)}</div>
      </section>

      <div className="overview-bottom section">
        <section className="surface quick-panel">
          <div className="quick-header"><div><span className="eyebrow">DISTRIBUCIÓN</span><h2>Versiones y código</h2></div><GitBranch size={21} /></div>
          {VERSIONES.slice(0, 4).map(item => <button key={item.channel} className="quick-row" type="button" onClick={() => navigate("versiones")}><span>{item.channel}</span><strong>{item.value}</strong><ChevronRight size={16} /></button>)}
        </section>
        <section className="surface quick-panel">
          <div className="quick-header"><div><span className="eyebrow">TRAZABILIDAD</span><h2>Últimas evidencias</h2></div><FileText size={21} /></div>
          {[...EVIDENCIAS].sort((a, b) => b.at.localeCompare(a.at)).slice(0, 3).map(item => <button key={item.id} type="button" className="quick-row evidence-quick" onClick={() => showEvidence(item)}><span><strong>{item.title}</strong><small>{item.source}</small></span><ChevronRight size={16} /></button>)}
        </section>
      </div>
    </>
  );
}

function EntradaFicha({ row, showEvidence }: { row: Ficha; showEvidence: (item: Evidencia) => void }) {
  const source = evidencia(row.evidence);
  return (
    <article className="fact-row">
      <div><span className="fact-category">{row.label ?? COMPONENTES[row.component]}</span><h3>{row.value}</h3><p>{row.detail}</p></div>
      <div className="fact-side">
        <Sello confidence={row.confidence} />
        {row.confidence === "confirmed" && (row.at || source) && <small>{fecha(row.at ?? source?.at, !!row.at || !source?.dateOnly)}</small>}
        {source && <button type="button" className="text-button" onClick={() => showEvidence(source)}>{row.confidence === "confirmed" ? "Ver evidencia" : "Ver referencia"} <ArrowUpRight size={14} /></button>}
      </div>
    </article>
  );
}

function Equipos({ route, navigate, showEvidence, openForm, records }: {
  route: Destino; navigate: Navegar; showEvidence: (e: Evidencia) => void;
  openForm: (request: SolicitudFormulario) => void; records: Registro[];
}) {
  const node = NODOS.find(item => item.id === route.host);
  const [confidence, setConfidence] = useState<Confianza | "all">("all");
  useEffect(() => setConfidence("all"), [route.host, route.component]);
  if (!node) return (
    <>
      <Encabezado kicker="INVENTARIO" title="Equipos" detail="Abre una ficha para ver componentes, estado, fuente y fecha de la última lectura." />
      <div className="surface inventory-tools">
        <div><span className="eyebrow">VISTA COMPARADA</span><h2>Componentes por equipo</h2></div>
        <label><span className="sr-only">Seleccionar componente para comparar</span><select value={route.component ?? "all"} onChange={event => navigate("equipos", undefined, event.target.value === "all" ? undefined : event.target.value as Componente)}><option value="all">Inventario general</option>{Object.entries(COMPONENTES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
      </div>
      {route.component ? (
        <div className="compare-nodes">
          {NODOS.map(item => <section className="surface facts" key={item.id}>
            <div className="compare-node-title"><h2>{item.name}</h2><button type="button" className="text-button" onClick={() => navigate("equipos", item.id, route.component)}>Abrir ficha <ArrowUpRight size={14} /></button></div>
            {item.rows.filter(row => row.component === route.component).map((row, i) => <EntradaFicha key={row.component + i} row={row} showEvidence={showEvidence} />)}
          </section>)}
        </div>
      ) : <div className="devices-grid inventory-grid">{NODOS.map(item => <TarjetaEquipo key={item.id} node={item} navigate={navigate} />)}</div>}
    </>
  );
  const Icon = deviceIcon(node);
  const rows = node.rows.filter(row => (!route.component || row.component === route.component) && (confidence === "all" || row.confidence === confidence));
  const nodeRecords = records.filter(row => row.host === node.id && row.kind === "verification");
  const newestManual = nodeRecords.find(row => row.state === "confirmed");
  return (
    <>
      <button className="text-button back-link" type="button" onClick={() => navigate("equipos")}><ArrowLeft size={16} /> Todos los equipos</button>
      <section className={"surface node-heading accent-" + node.accent}>
        <div className="node-identity"><span className="device-icon"><Icon size={28} strokeWidth={1.7} /></span><div><span className="eyebrow">{node.tag}</span><h1>{node.name}</h1><p>{node.system} · {node.description}</p></div></div>
        <EstadoFleet node={node} />
        <div className="node-dates">
          <span><Clock3 size={16} /><span><small>{node.id === "cloud" ? "Última lectura del SFTP" : "Última lectura de infraestructura"}</small><strong>{fecha(node.checkedAt)}</strong></span></span>
          <span><ClipboardCheck size={16} /><span><small>Última verificación manual</small><strong>{fecha(newestManual?.createdAt)}</strong></span></span>
          <button type="button" className="button subtle" onClick={() => openForm({ kind: "verification", host: node.id, category: route.component ?? "system" })}><Plus size={16} /> Registrar verificación</button>
        </div>
      </section>
      <section className="section">
        <TituloSeccion title="Componentes" detail="La confianza de cada dato se indica en su propia ficha." />
        <div className="filters component-chips" aria-label="Componentes del equipo">
          <button className={"chip " + (!route.component ? "chosen" : "")} type="button" aria-pressed={!route.component} onClick={() => navigate("equipos", node.id)}>Todos</button>
          {[...new Set(node.rows.map(row => row.component))].map(key => <button key={key} className={"chip " + (route.component === key ? "chosen" : "")} type="button" aria-pressed={route.component === key} onClick={() => navigate("equipos", node.id, key)}>{COMPONENTES[key]}</button>)}
        </div>
        <div className="status-filters" aria-label="Nivel de verificación">
          {([["all", "Todo"], ["confirmed", "Hechos confirmados"], ["assumed", "Supuestos"], ["pending", "Verificaciones pendientes"]] as const).map(([id, label]) =>
            <button className={confidence === id ? "active" : ""} type="button" key={id} aria-pressed={confidence === id} onClick={() => setConfidence(id)}>{label}</button>)}
        </div>
        {rows.length > 0 ? <div className="surface facts">{rows.map((row, i) => <EntradaFicha key={row.component + i} row={row} showEvidence={showEvidence} />)}</div> : <div className="surface empty">Sin datos de ese nivel para este componente.</div>}
      </section>
      {nodeRecords.some(row => !route.component || row.category === route.component) && <section className="section">
        <TituloSeccion title="Verificaciones registradas" detail="Observaciones manuales añadidas a la bitácora." />
        <div className="surface facts">{nodeRecords.filter(row => !route.component || row.category === route.component).map(row => <article className="fact-row" key={row.id}><div><span className="fact-category">{etiquetaCategoria(row.category)}</span><h3>{row.title}</h3><p className="preserve-text">{row.detail}</p>{row.evidence && <p className="reference">{row.evidence}</p>}</div><div className="fact-side"><Sello confidence={row.state as Confianza} /><small>{fecha(row.createdAt)}</small></div></article>)}</div>
      </section>}
    </>
  );
}

function Versiones({ showEvidence, navigate }: { showEvidence: (item: Evidencia) => void; navigate: Navegar }) {
  return (
    <>
      <Encabezado kicker="DISTRIBUCIÓN Y TRAZABILIDAD" title="Versiones y código" detail="Contrasta la versión instalada, el índice APT firmado, la rama y la Release de GitHub, y las referencias de código del laboratorio." />
      <div className="version-summary">
        <div className="surface summary-cell"><GitBranch size={22} /><div><span>Versión del central</span><strong>{VERSION_PRODUCCION}</strong></div></div>
        <div className="surface summary-cell"><ShieldCheck size={22} /><div><span>APT público</span><strong>No confirmado</strong></div></div>
        <div className="surface summary-cell"><FileText size={22} /><div><span>GitHub · última Release</span><strong>No confirmado</strong></div></div>
      </div>
      <section className="section">
        <TituloSeccion title="Matriz de distribución" detail="El alcance y la evidencia se verifican por canal." />
        <div className="surface table-scroll">
          <table className="matrix"><caption className="sr-only">Canales, referencias y estado de verificación</caption>
            <thead><tr><th scope="col">Canal</th><th scope="col">Versión o referencia</th><th scope="col">Verificación y alcance</th><th scope="col">Fuente</th></tr></thead>
            <tbody>{VERSIONES.map(item => {
              const source = evidencia(item.evidence);
              return <tr key={item.channel}><th scope="row"><strong>{item.channel}</strong><small>{item.scope}</small></th><td><span className="version-value">{item.value}</span></td><td><Sello confidence={item.confidence} /><p>{item.note}</p></td><td>{source && <button type="button" className="text-button" onClick={() => showEvidence(source)}>Evidencia <ArrowUpRight size={14} /></button>}</td></tr>;
            })}</tbody>
          </table>
        </div>
      </section>
      <section className="section">
        <TituloSeccion title="Versión por equipo" />
        <div className="surface quick-panel">{NODOS.map(node => <button className="quick-row" type="button" key={node.id} onClick={() => navigate("equipos", node.id, "installed")}><span>{node.name}</span><strong>{node.installed}</strong><ChevronRight size={16} /></button>)}</div>
      </section>
    </>
  );
}

export function Dashboard() {
  const [route, setRoute] = useState<Destino>({ view: "resumen" });
  const [records, setRecords] = useState<Registro[]>([]);
  const [cursor, setCursor] = useState<Cursor | null>(null);
  const [loading, setLoading] = useState(true);
  const [journalError, setJournalError] = useState("");
  const [notice, setNotice] = useState("");
  const [form, setForm] = useState<SolicitudFormulario | null>(null);
  const [visibleEvidence, setVisibleEvidence] = useState<Evidencia | null>(null);
  const pendingSave = useRef<{ content: string; id: string } | null>(null);

  useEffect(() => {
    const sync = () => setRoute(leerRuta());
    sync();
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => { window.removeEventListener("hashchange", sync); window.removeEventListener("popstate", sync); };
  }, []);

  const navigate: Navegar = (view, host, component) => {
    const nextHash = "#" + [view, host, component].map(value => value ?? "").join("/").replace(/\/+$/, "");
    if (window.location.hash !== nextHash) window.history.pushState(null, "", nextHash);
    setRoute({ view, host, component });
    if (view !== route.view || host !== route.host) window.scrollTo({ top: 0 });
  };

  const refresh = useCallback(async (page?: Cursor) => {
    setLoading(true);
    setJournalError("");
    try {
      const url = "/api/bitacora" + (page ? "?" + new URLSearchParams(page).toString() : "");
      const response = await fetch(url, { cache: "no-store" });
      const data = await response.json() as JournalPage;
      if (!response.ok || !Array.isArray(data.entries)) throw new Error(data.error ?? "No se pudo consultar la bitácora.");
      const entries = data.entries;
      setRecords(existing => page ? [...existing, ...entries.filter(item => !existing.some(row => row.id === item.id))] : entries);
      setCursor(data.next ?? null);
    } catch (error) {
      setJournalError(error instanceof Error ? error.message : "No se pudo consultar la bitácora.");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const save: Guardar = async (body: Borrador) => {
    const content = JSON.stringify(body);
    const id = pendingSave.current?.content === content ? pendingSave.current.id : crypto.randomUUID();
    pendingSave.current = { content, id };
    const response = await fetch("/api/bitacora", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...body, requestId: id }) });
    const data = await response.json() as { entry?: Registro; error?: string };
    if (!response.ok || !data.entry) throw new Error(data.error ?? "No se pudo guardar el registro.");
    const entry = data.entry;
    if (pendingSave.current?.id === id) pendingSave.current = null;
    setRecords(existing => [entry, ...existing.filter(row => row.id !== id)]);
    setNotice(body.kind === "incident" ? "Incidencia registrada en la bitácora privada." : "Registro guardado en la bitácora privada.");
    setForm(null);
  };

  const currentMenu = menus.find(item => item.id === route.view);
  const journalBar = <div className="journal-bar"><span role="status">{journalError || (loading ? "Consultando la bitácora…" : "Bitácora privada · registros manuales")}</span><button type="button" className="button subtle small" onClick={() => void refresh()} disabled={loading}><RefreshCw size={15} /> Recargar registros</button></div>;
  const more = cursor && <button type="button" className="button subtle" disabled={loading} onClick={() => void refresh(cursor)}>Cargar registros anteriores</button>;

  return (
    <div className="app-shell">
      <a href="#main-content" className="skip">Ir al contenido</a>
      <aside className="sidebar">
        <button className="brand" type="button" onClick={() => navigate("resumen")}><Marca /><span><strong>FrameBridge</strong><small>LABORATORIO LINUX</small></span></button>
        <div className="nav-label">ESPACIO DE TRABAJO</div>
        <nav className="navigation" aria-label="Navegación principal">
          {menus.map(item => <button key={item.id} type="button" aria-current={route.view === item.id ? "page" : undefined} onClick={() => navigate(item.id)}><item.icon size={19} strokeWidth={1.75} /><span>{item.label}</span>{route.view === item.id && <ChevronRight className="nav-chevron" size={15} />}</button>)}
        </nav>
        <div className="sidebar-bottom"><div className="rail-status"><ShieldCheck size={20} /><span><strong>Supervisión manual</strong><small>Lecturas con fecha de corte</small></span></div></div>
      </aside>
      <div className="workspace">
        <div className="topbar"><div className="breadcrumb"><span>Laboratorio</span><ChevronRight size={14} /><strong>{currentMenu?.label}</strong></div><div className="top-actions"><span className="private-pill"><LockKeyhole size={14} /> Privado</span><button className="button primary" type="button" onClick={() => setForm({ kind: "incident", host: route.host ?? EQUIPOS[0].id })}><Plus size={17} /> Nueva incidencia</button></div></div>
        <main id="main-content" className="content">
          {route.view === "resumen" && <Resumen navigate={navigate} showEvidence={setVisibleEvidence} records={records} journalReady={!loading && !journalError && !cursor} />}
          {route.view === "equipos" && <Equipos route={route} navigate={navigate} showEvidence={setVisibleEvidence} openForm={setForm} records={records} />}
          {route.view === "versiones" && <Versiones showEvidence={setVisibleEvidence} navigate={navigate} />}
          {route.view === "historial" && <><Historial records={records} showEvidence={setVisibleEvidence} openForm={setForm} initialComponent={route.component} />{journalBar}{more}</>}
          {route.view === "incidencias" && <><Incidencias records={records} openForm={setForm} />{journalBar}{more}</>}
          {route.view === "checklist" && <Checklist save={save} />}
          <footer className="footer"><span>FrameBridge · Supervisión privada</span><span><Activity size={14} /> Corte inicial · {fecha(CORTE, false)}</span></footer>
        </main>
      </div>
      {form && <Formulario request={form} save={save} close={() => setForm(null)} />}
      {visibleEvidence && <Modal title={visibleEvidence.title} kicker="EVIDENCIA" close={() => setVisibleEvidence(null)}>
        <div className="evidence-body"><Sello confidence={visibleEvidence.confidence ?? "confirmed"} /><dl><div><dt>Fuente</dt><dd>{visibleEvidence.source}</dd></div><div><dt>Fecha del registro</dt><dd>{fecha(visibleEvidence.at, !visibleEvidence.dateOnly)}</dd></div></dl><p>{visibleEvidence.detail}</p>
          {visibleEvidence.reference && <p className="reference">{visibleEvidence.reference}</p>}
          {visibleEvidence.url && <a href={visibleEvidence.url} className="button subtle" target="_blank" rel="noreferrer">Abrir fuente <ExternalLink size={15} /></a>}
        </div>
      </Modal>}
      {notice && <div className="toast" role="status"><Check size={18} /><span>{notice}</span><button type="button" className="icon-button" aria-label="Cerrar aviso" onClick={() => setNotice("")}><X size={17} /></button></div>}
    </div>
  );
}
