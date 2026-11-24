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
 * Journeys: faint lines show how each person who has died moved, from the
 * place of one record to the next in date order (fha site works the stops
 * out; this file only draws them). Picking a person in the "Show one
 * person's moves" list, or the Show moves button beside their name in a
 * pin's panel, draws their line in the accent colour, numbers the stops,
 * puts an arrow halfway along each move, and lists the stops as text in
 * the panel, the form a screen reader gets. Each move is drawn
 * as the shortest route over the globe, so a crossing from Europe curves.
 * Past FAINT_MAX journeys the faint lines are left out (too many to read,
 * and slow); the picked one still draws.
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
  var FAINT_MAX = 200;     // journeys past which no faint lines are drawn
  var FAINT_WIDTH = 1.5;   // px on screen, a faint journey line
  var FOCUS_WIDTH = 3;     // px on screen, the followed journey's line

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

  /* A stop's years: "1840", or "1870 to 1900" for several records in a row there. */
  function stopYears(stop) {
    return stop.year_from === stop.year_to
      ? String(stop.year_from) : stop.year_from + ' to ' + stop.year_to;
  }

  /* One stop as a line of text: "Fairview, 1870 to 1900, census, residence". */
  function stopText(stop, place) {
    return [place ? place.name : stop.place, stopYears(stop)].concat(stop.what).join(', ');
  }

  /* The moves of one journey, each from one stop's pin to the next as
   * [lon, lat] pairs, with the year of the later stop (the move happened
   * by then). A stop whose pin is missing is skipped. */
  function journeySegments(journey, byId) {
    var points = [];
    journey.stops.forEach(function (stop) {
      var p = byId[stop.place];
      if (p) points.push({at: [p.lon, p.lat], year: stop.year_from});
    });
    var segments = [];
    for (var i = 1; i < points.length; i++) {
      segments.push({from: points[i - 1].at, to: points[i].at, year: points[i].year});
    }
    return segments;
  }

  /* The numbers to write beside each pin of a followed journey: a place
   * the person came back to carries both ("1, 3"). [{place, label}] in
   * first-visit order. */
  function stopNumbers(journey) {
    var order = [];
    var numbers = {};
    journey.stops.forEach(function (stop, i) {
      if (!numbers[stop.place]) {
        numbers[stop.place] = [];
        order.push(stop.place);
      }
      numbers[stop.place].push(i + 1);
    });
    return order.map(function (id) { return {place: id, label: numbers[id].join(', ')}; });
  }

  /* What a journey's line leaves out, as a sentence, or ''. */
  function leftOutText(journey) {
    var parts = [];
    if (journey.undated) parts.push(plural(journey.undated, 'event') + ' with no date');
    if (journey.unplaced) {
      parts.push((journey.undated ? journey.unplaced : plural(journey.unplaced, 'event'))
        + ' at a place with no map position yet');
    }
    return parts.length ? 'Not on this line: ' + parts.join(' and ') + '.' : '';
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
  function clear(panel) {
    while (panel.firstChild) panel.removeChild(panel.firstChild);
  }

  /* `follow(href)` is given when some people have journeys: it returns a
   * function that draws that person's line, or null when they have none. */
  function showPlace(panel, place, follow) {
    clear(panel);
    panel.appendChild(el('h2', place.name));
    if (place.label && place.label !== place.name) panel.appendChild(el('p', place.label, 'note'));
    panel.appendChild(el('p', plural(place.events, 'event') + " in the family's records."));
    if (place.people.length) {
      var list = el('ul', null, 'map-people');
      place.people.forEach(function (person) {
        var li = el('li');
        li.appendChild(link(person.href, person.name));
        var go = follow && follow(person.href);
        if (go) {
          var button = el('button', 'Show moves', 'map-follow');
          button.setAttribute('type', 'button');
          button.setAttribute('aria-label', 'Show ' + person.name + "'s moves on the map");
          button.addEventListener('click', go);
          li.appendChild(document.createTextNode(' '));
          li.appendChild(button);
        }
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

  /* Fill the panel with one person's journey: who, their years, each stop
   * in order (the same numbers the map shows), what the line leaves out. */
  function showJourney(panel, journey, byId) {
    clear(panel);
    panel.appendChild(el('h2', journey.name));
    if (journey.life) panel.appendChild(el('p', journey.life, 'note'));
    var list = el('ol', null, 'map-stops');
    journey.stops.forEach(function (stop) {
      list.appendChild(el('li', stopText(stop, byId[stop.place])));
    });
    panel.appendChild(list);
    var left = leftOutText(journey);
    if (left) panel.appendChild(el('p', left, 'note'));
    var p = el('p');
    p.appendChild(link(journey.href, 'Open the page for ' + journey.name));
    panel.appendChild(p);
  }

  function draw(d3, topojson, world, data, box, panel, reset, picker) {
    var places = data.places.slice().sort(function (a, b) { return b.events - a.events; });
    var journeys = data.journeys || [];
    var byId = {};
    data.places.forEach(function (p) { byId[p.id] = p; });
    var projection = makeProjection(d3, data.places, VIEW_W, VIEW_H);
    var path = d3.geoPath(projection);
    var svg = d3.select(box).append('svg')
      .attr('class', 'map-svg')
      .attr('viewBox', '0 0 ' + VIEW_W + ' ' + VIEW_H)
      .attr('preserveAspectRatio', 'xMidYMid meet');
    // The arrow halfway along each move of a followed journey. Sized by the
    // line's width, which the zoom keeps constant on screen.
    svg.append('defs').append('marker')
      .attr('id', 'fha-map-arrow')
      .attr('viewBox', '0 0 10 10')
      .attr('refX', 5).attr('refY', 5)
      .attr('markerWidth', 4).attr('markerHeight', 4)
      .attr('orient', 'auto')
      .append('path').attr('class', 'map-arrow').attr('d', 'M0,0L10,5L0,10z');
    var layer = svg.append('g');
    var k = 1;   // the zoom's scale, so lines and labels drawn later match it
    layer.append('path').attr('class', 'map-land')
      .attr('d', path(topojson.feature(world, world.objects.land)));
    layer.append('path').attr('class', 'map-borders')
      .attr('d', path(topojson.mesh(world, world.objects.countries, function (a, b) { return a !== b; })));

    // Everyone's journeys, faint, under the pins.
    var faint = layer.append('g').attr('class', 'map-journeys');
    if (journeys.length <= FAINT_MAX) {
      journeys.forEach(function (j) {
        var segs = journeySegments(j, byId);
        if (!segs.length) return;
        faint.append('path').attr('class', 'map-journey')
          .attr('stroke-width', FAINT_WIDTH)
          .attr('d', path({type: 'MultiLineString',
                           coordinates: segs.map(function (s) { return [s.from, s.to]; })}));
      });
    }
    var focus = layer.append('g').attr('class', 'map-journey-focus');

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
    // The followed journey's stop numbers, above the pins.
    var numbers = layer.append('g').attr('class', 'map-stop-numbers');

    function at(lonLat, scale) {
      var xy = projection(lonLat);
      return 'translate(' + xy[0] + ',' + xy[1] + ') scale(' + scale + ')';
    }

    var followed = -1;
    function follow(index) {
      followed = index;
      focus.selectAll('*').remove();
      numbers.selectAll('*').remove();
      svg.classed('map-following', index >= 0);
      if (picker) picker.value = index >= 0 ? String(index) : '';
      if (index < 0) return;
      var j = journeys[index];
      pins.classed('is-active', false);
      // Each move in two halves, the first ending in an arrow, so the
      // arrow sits halfway along the route and not under a pin.
      journeySegments(j, byId).forEach(function (s) {
        var mid = d3.geoInterpolate(s.from, s.to)(0.5);
        focus.append('path').attr('class', 'map-journey is-followed')
          .attr('stroke-width', FOCUS_WIDTH / k).attr('marker-end', 'url(#fha-map-arrow)')
          .attr('d', path({type: 'LineString', coordinates: [s.from, mid]}));
        focus.append('path').attr('class', 'map-journey is-followed')
          .attr('stroke-width', FOCUS_WIDTH / k)
          .attr('d', path({type: 'LineString', coordinates: [mid, s.to]}));
      });
      stopNumbers(j).forEach(function (n) {
        var p = byId[n.place];
        if (!p) return;
        numbers.append('g').datum(p).attr('transform', at([p.lon, p.lat], 1 / k))
          .append('text').attr('class', 'map-stop-number')
          .attr('y', -(pinRadius(p.events) + 4))
          .text(n.label);
      });
      showJourney(panel, j, byId);
    }

    // Who has a journey, by the href their name links to.
    var journeyOf = {};
    journeys.forEach(function (j, i) { journeyOf[j.href] = i; });
    function followButton(href) {
      if (!(href in journeyOf)) return null;
      return function () { follow(journeyOf[href]); };
    }

    if (picker && journeys.length) {
      if (journeys.length > FAINT_MAX && picker.options.length) {
        picker.options[0].textContent = 'Choose a person';
      }
      journeys.forEach(function (j, i) {
        var option = el('option', j.life ? j.name + ' (' + j.life + ')' : j.name);
        option.value = String(i);
        picker.appendChild(option);
      });
      picker.addEventListener('change', function () {
        follow(picker.value === '' ? -1 : Number(picker.value));
      });
      if (picker.parentNode) picker.parentNode.hidden = false;
    }

    function choose(d) {
      if (followed >= 0) follow(-1);
      pins.classed('is-active', function (x) { return x === d; });
      showPlace(panel, d, journeys.length ? followButton : null);
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
      k = event.transform.k;
      layer.attr('transform', event.transform);
      pins.attr('transform', function (d) { return at([d.lon, d.lat], 1 / k); });
      numbers.selectAll('g').attr('transform', function (d) { return at([d.lon, d.lat], 1 / k); });
      faint.selectAll('path').attr('stroke-width', FAINT_WIDTH / k);
      focus.selectAll('path').attr('stroke-width', FOCUS_WIDTH / k);
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
                      makeProjection: makeProjection, pinRadius: pinRadius,
                      stopYears: stopYears, stopText: stopText, journeySegments: journeySegments,
                      stopNumbers: stopNumbers, leftOutText: leftOutText};
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
         document.getElementById('fha-map-reset'), document.getElementById('fha-map-journey'));
  });
})();
