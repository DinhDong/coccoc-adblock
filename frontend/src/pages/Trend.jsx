import {
  ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid,
  Tooltip, Legend,
} from "recharts";
import { CHART, trendSeries, runSeries } from "../analytics.js";

export default function Trend({ tickets }) {
  const days = trendSeries(tickets);
  const runs = runSeries(tickets);
  return (
    <div className="ad-content">
      <div className="ad-pagehead">
        <div><h1>Trend</h1><p>Volume and speed over the last 14 days.</p></div>
      </div>

      <div className="ad-panel" style={{ marginTop: 18 }}>
        <h3>Reports created vs completed per day</h3>
        <div className="ad-chartbox">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={days} margin={{ top: 8, right: 12, bottom: 0, left: -24 }}>
              <CartesianGrid stroke={CHART.grid} strokeDasharray="3 3" />
              <XAxis dataKey="label" tick={CHART.tick} tickLine={false} interval={1} />
              <YAxis tick={CHART.tick} tickLine={false} allowDecimals={false} />
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 12, fontFamily: "Arial, Helvetica, sans-serif" }} />
              <Line type="monotone" dataKey="created" name="Created" stroke={CHART.green} strokeWidth={2} dot={{ r: 2 }} />
              <Line type="monotone" dataKey="completed" name="Completed" stroke={CHART.deep} strokeWidth={2} dot={{ r: 2 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="ad-panel" style={{ marginTop: 16 }}>
        <h3>Pipeline run time per report</h3>
        <div className="ad-chartbox">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={runs} margin={{ top: 8, right: 12, bottom: 0, left: -18 }}>
              <CartesianGrid stroke={CHART.grid} strokeDasharray="3 3" />
              <XAxis dataKey="name" tick={CHART.tick} tickLine={false} />
              <YAxis tick={CHART.tick} tickLine={false} unit="s" />
              <Tooltip formatter={(v) => [v + " s", "Run time"]} />
              <Line type="monotone" dataKey="seconds" stroke={CHART.orange} strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
