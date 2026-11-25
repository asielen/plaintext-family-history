/* fha-map.js: the family map (map.html).
 *
 * A cousin opens map.html and sees an outline of the world, zoomed to where
 * the family's records put people, with a pin at each place. Clicking a pin
 * (or pressing Enter on it) shows the place's name, how many events in the
 * family's records happened there and who they place there, with links to their pages and to the place page. It
 * works from a USB stick with no internet: the outline is vendor/fha-world.js
 * (Natural Earth 1:110m, as window.FHA_WORLD), drawn with the vendored D3 and
 * topojson-client, and the pins are the JSON fha site writes into the page
 * (<script type="application/json" id="fha-map-data">). Nothing is fetched or
 * stored. Without script the page's own list of places stands in for the map.
 *
 * Projection: Mercator for a family in one region (the shape OpenStreetMap
 * shows), Natural Earth when the pins span more than 60 degrees of longitude
 * or 40 of latitude. The view is fitted to the pins, widened to at least
 * 5 degrees each way so one pin, or a few in one town, does not zoom in
 * without end. Pins either side of the date line are drawn the long way
 * round the world (a known limit). With a dozen pins or fewer each carries
 * its place name; past that the names would cover each other, so a name
 * shows on the pin chosen or pointed at.
 *
 * Text from the data goes in as text nodes and attributes, never as HTML.
 * Also loadable from node (module.exports), so the build's tests can check
 * the projection maths against the real D3. */
(function () {
  'use strict';

  var VIEW_W = 960;
  var VIEW_H = 540;
  var PAD = 40;            // px kept clear round the pins
  var MIN_HALF_SPAN = 5;   // degrees each way from the pins' centre
  var WIDE_LON = 60;       // degrees of longitude past which the world projection is used
  var WIDE_LAT = 40;       // the same for latitude
  var MAX_PIN = 12;        // px, the biggest pin
  var LABEL_ALL = 12;      // pins up to which every pin shows its name
  var MERCATOR_LAT = 85;   // Mercator cannot show the poles

  /* The pins' extent in degrees, or null when there are none. */
  function bounds(places) {
    if (!places || !places.length) return null;
    var b = {minLat: 90, maxLat: -90, minLon: 180, maxLon: -180};
    places.forEach(function (p) {
      b.minLat = Math.min(b.minLat, p.lat);
      b.maxLat = Math.max(b.maxLat, p.lat);
      b.minLon = Math.min(b.minLon, p.lon);
      b.maxLon = Math.max(b.maxLon, p.lon);
    });
    return b;
  }

  /* 'mercator' for pins in one region, else 'naturalEarth'. */
  function projectionKind(places) {
    var b = bounds(places);
    if (!b) return 'naturalEarth';
    return (b.maxLon - b.minLon > WIDE_LON || b.maxLat - b.minLat > WIDE_LAT)
      ? 'naturalEarth' : 'mercator';
  }

  /* What the view is fitted to: the corners of the pins' extent, widened to
   * at least MIN_HALF_SPAN degrees each way round its centre. The whole
   * world when there are no pins. */
  function fitTarget(places, kind) {
    var b = bounds(places);
    if (!b) return {type: 'Sphere'};
    var cLat = (b.minLat + b.maxLat) / 2;
    var cLon = (b.minLon + b.maxLon) / 2;
    var hLat = Math.max((b.maxLat - b.minLat) / 2, MIN_HALF_SPAN);
    var hLon = Math.max((b.maxLon - b.minLon) / 2, MIN_HALF_SPAN);
    var lo = Math.max(cLat - hLat, -90);
    var hi = Math.min(cLat + hLat, 90);
    if (kind === 'mercator') {
      lo = Math.max(lo, -MERCATOR_LAT);
      hi = Math.min(hi, MERCATOR_LAT);
    }
    var w = cLon - hLon;
    var e = cLon + hLon;
    return {type: 'MultiPoint', coordinates: [[w, lo], [e, lo], [w, hi], [e, hi]]};
  }

  /* A D3 projection fitted to the pins inside a w x h frame. */
  function makeProjection(d3, places, w, h) {
    var kind = projectionKind(places);
    var projection = kind === 'mercator' ? d3.geoMercator() : d3.geoNaturalEarth1();
    return projection.fitExtent([[PAD, PAD], [w - PAD, h - PAD]], fitTarget(places, kind));
  }

  /* A pin's radius in px: bigger for a place with more records, at most MAX_PIN. */
  function pinRadius(events) {
    return Math.min(MAX_PIN, 6 + Math.sqrt(Math.max(1, events || 0)));
  }

  function plural(n, word) {
    return n + ' ' + word + (n === 1 ? '' : 's');
  }

  function el(tag, text, cls) {
    var node = document.createElement(tag);
    if (text != null) node.appendChild(document.createTextNode(text));
    if (cls) node.className = cls;
    return node;
  }

  function link(href, text) {
    var a = el('a', text);
    a.setAttribute('href', href);
    return a;
  }

  /* Fill the panel beside the map with one place: its name, where it is,
   * how many records put family there, the people, and its page. */
  function showPlace(panel, place) {
    while (panel.firstChild) panel.removeChild(panel.firstChild);
    panel.appendChild(el('h2', place.name));
    if (place.label && place.label !== place.name) panel.appendChild(el('p', place.label, 'note'));
    panel.appendChild(el('p', plural(place.events, 'event') + " in the family's records."));
    if (place.people.length) {
      var list = el('ul', null, 'map-people');
      place.people.forEach(function (person) {
        var li = el('li');
        li.appendChild(link(person.href, person.name));
        list.appendChild(li);
      });
      panel.appendChild(list);
      if (place.more_people) {
        var more = place.more_people;
        panel.appendChild(el('p', 'and ' + more + (more === 1 ? ' more person' : ' more people')
          + ' on the place page.', 'note'));
      }
    }
    var p = el('p');
    p.appendChild(link(place.href, 'Open the page for ' + place.name));
    panel.appendChild(p);
  }

  function draw(d3, topojson, world, data, box, panel, reset) {
    var places = data.places.slice().sort(function (a, b) { return b.events - a.events; });
    var projection = makeProjection(d3, data.places, VIEW_W, VIEW_H);
    var path = d3.geoPath(projection);
    var svg = d3.select(box).append('svg')
      .attr('class', 'map-svg')
      .attr('viewBox', '0 0 ' + VIEW_W + ' ' + VIEW_H)
      .attr('preserveAspectRatio', 'xMidYMid meet');
    var layer = svg.append('g');
    layer.append('path').attr('class', 'map-land')
      .attr('d', path(topojson.feature(world, world.objects.land)));
    layer.append('path').attr('class', 'map-borders')
      .attr('d', path(topojson.mesh(world, world.objects.countries, function (a, b) { return a !== b; })));

    // Biggest first, so a small pin is never hidden under a big one.
    var pins = layer.append('g').selectAll('g').data(places).enter().append('g')
      .attr('class', 'map-pin')
      .attr('tabindex', 0)
      .attr('role', 'button')
      .attr('aria-label', function (d) { return d.name + ': ' + plural(d.events, 'event'); })
      .attr('transform', function (d) {
        var xy = projection([d.lon, d.lat]);
        return 'translate(' + xy[0] + ',' + xy[1] + ')';
      });
    pins.append('circle').attr('r', function (d) { return pinRadius(d.events); });
    pins.append('title').text(function (d) { return d.name + ': ' + plural(d.events, 'event'); });
    pins.append('text')
      .attr('class', 'map-label')
      .attr('x', function (d) { return pinRadius(d.events) + 3; })
      .attr('dy', '0.35em')
      .text(function (d) { return d.name; });
    svg.classed('map-few', places.length <= LABEL_ALL);

    function choose(d) {
      pins.classed('is-active', function (x) { return x === d; });
      showPlace(panel, d);
    }
    pins.on('click', function (event, d) { choose(d); });
    pins.on('keydown', function (event, d) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        choose(d);
      }
    });

    // Pins and lines keep their size on screen as the map zooms.
    var zoom = d3.zoom().scaleExtent([1, 60]).on('zoom', function (event) {
      var k = event.transform.k;
      layer.attr('transform', event.transform);
      pins.attr('transform', function (d) {
        var xy = projection([d.lon, d.lat]);
        return 'translate(' + xy[0] + ',' + xy[1] + ') scale(' + (1 / k) + ')';
      });
    });
    svg.call(zoom);
    if (reset) {
      reset.hidden = false;
      reset.addEventListener('click', function () {
        svg.call(zoom.transform, d3.zoomIdentity);
      });
    }
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {bounds: bounds, projectionKind: projectionKind, fitTarget: fitTarget,
                      makeProjection: makeProjection, pinRadius: pinRadius};
    return;
  }

  document.addEventListener('DOMContentLoaded', function () {
    var box = document.getElementById('fha-map');
    var source = document.getElementById('fha-map-data');
    var panel = document.getElementById('fha-map-panel');
    if (!box || !source || !panel) return;
    // A copy missing a vendored file keeps the list under the map.
    if (!window.d3 || !window.topojson || !window.FHA_WORLD) return;
    var data;
    try {
      data = JSON.parse(source.textContent);
    } catch (e) {
      return;
    }
    if (!data || !data.places || !data.places.length) return;
    box.hidden = false;
    panel.hidden = false;
    draw(window.d3, window.topojson, window.FHA_WORLD, data, box, panel,
         document.getElementById('fha-map-reset'));
  });
})();
