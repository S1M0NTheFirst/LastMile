import "./globals.css";

export const metadata = {
  title: "LastMile Simulator",
  description: "Robot swarm simulator",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
