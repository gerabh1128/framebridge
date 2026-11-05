import { Check, CircleHelp, Clock3 } from "lucide-react";
import { CONFIANZA } from "./registro";
import type { Confianza } from "./registro";

export function Sello({ confidence, short = false }: { confidence: Confianza | "na"; short?: boolean }) {
  const Icon = confidence === "confirmed" ? Check : confidence === "assumed" ? CircleHelp : Clock3;
  return (
    <span className={"badge " + confidence}>
      {confidence !== "na" && <Icon size={13} aria-hidden="true" />}
      {confidence === "na" ? "No aplica" : CONFIANZA[confidence]}
      {!short && (confidence === "assumed" || confidence === "pending") && <span> · no confirmado</span>}
    </span>
  );
}

export function Encabezado({ kicker, title, detail, children }: { kicker: string; title: string; detail: string; children?: React.ReactNode }) {
  return <header className="page-heading"><div><span className="eyebrow">{kicker}</span><h1>{title}</h1><p>{detail}</p></div>{children}</header>;
}
