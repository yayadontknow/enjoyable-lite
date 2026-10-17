// Enjoyable Lite Web settings. The Firebase web config is public by design: Firestore rules allow reading the
// descriptions only, and only the Cloud Run service writes them.
window.ENJOYABLE = {
  api: "", // same origin: Firebase Hosting sends /api/** to the Cloud Run service
  firebase: {
    apiKey: "AIzaSyDbvqYJv_rfdQUJS6uIwlrQ3Y5Qei-vUmo",
    authDomain: "paidpaid.firebaseapp.com",
    projectId: "paidpaid",
    appId: "1:549536487427:web:17bd597ae354372aa99636",
  },
  // ready to play at once (prepared in advance with exact timing)
  demos: [
    { id: "FsHWmOPa-44", title: "Harry Meets Buckbeak", sub: "Harry Potter · descriptions in the pauses", len: "7 min" },
    { id: "YIgT1jVD3TQ", title: "Queen of Tears, Ep 4", sub: "Korean drama · voice-over in your language", len: "3 min" },
  ],
};
// local development (the FastAPI server serves this folder): no Firestore, ask the server instead
if (["localhost", "127.0.0.1"].includes(location.hostname)) delete window.ENJOYABLE.firebase;
