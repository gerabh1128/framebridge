import {
  ACCIONES, CATEGORIAS_INCIDENCIA, COMPONENTES, CONFIANZA, EQUIPOS, ESTADOS_INCIDENCIA,
} from "./app/registro";

process.stdout.write(JSON.stringify({
  hosts: EQUIPOS.map(host => host.id),
  components: Object.keys(COMPONENTES),
  actions: Object.keys(ACCIONES),
  confidences: Object.keys(CONFIANZA),
  incidentStates: Object.keys(ESTADOS_INCIDENCIA),
  incidentCategories: CATEGORIAS_INCIDENCIA,
}));
