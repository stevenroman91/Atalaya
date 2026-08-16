// Mapa Leaflet de incidentes (nivel ciudad; barrio cuando se conoce).
(function () {
  var el = document.getElementById("map");
  if (!el || typeof L === "undefined") return;
  var map = L.map("map").setView([10, -80], 3);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  // Se reenvían TODOS los filtros de la página, no solo «scope»: el mapa
  // debe mostrar lo mismo que la lista. Con solo «scope», filtrar por otro
  // país dejaba el mapa buscando en los países por defecto del usuario.
  var qs = window.location.search.replace(/^\?/, "");
  var url = "/dashboard/map.json" + (qs ? "?" + qs : "");
  fetch(url)
    .then(function (r) { return r.json(); })
    .then(function (geo) {
      var layer = L.geoJSON(geo, {
        pointToLayer: function (feature, latlng) {
          var isAlert = feature.properties.type === "ALERTA";
          return L.circleMarker(latlng, {
            radius: 8,
            color: isAlert ? "#c0392b" : "#2471a3",
            fillColor: isAlert ? "#e74c3c" : "#3498db",
            fillOpacity: 0.75,
            weight: 2,
          });
        },
        onEachFeature: function (feature, l) {
          var p = feature.properties;
          l.bindPopup(
            "<strong>" + p.title + "</strong><br>" + p.place +
            (p.date ? "<br>" + p.date : "")
          );
        },
      }).addTo(map);
      var bounds = layer.getBounds();
      if (bounds.isValid()) map.fitBounds(bounds.pad(0.3));
    })
    .catch(function () { /* mapa vacío si falla la carga */ });
})();
