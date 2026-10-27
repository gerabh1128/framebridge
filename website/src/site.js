"use strict";
document.querySelectorAll(".codebox pre").forEach(function(pre){
  if(!navigator.clipboard || !window.isSecureContext) return;
  var button=document.createElement("button");
  button.type="button";button.className="copy-button";button.textContent="Copy";
  button.setAttribute("aria-label","Copy command block");
  button.addEventListener("click",async function(){
    try{await navigator.clipboard.writeText(pre.textContent);button.textContent="Copied";setTimeout(function(){button.textContent="Copy"},1800)}
    catch(error){button.textContent="Select to copy";setTimeout(function(){button.textContent="Copy"},2000)}
  });
  pre.parentElement.appendChild(button);
});
